#!/usr/bin/env python3
"""Dirty-worktree, conflict, and rebase handling for repository reconciliation."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reconcile_git import git_run, tail


@dataclass(frozen=True)
class LocalSnapshot:
    ref: str
    untracked: tuple[str, ...]


def nul_paths(output: str) -> tuple[str, ...]:
    return tuple(path for path in output.split("\0") if path)


def unmerged_stages(repo: Path) -> dict[str, set[int]]:
    result = git_run(repo, "ls-files", "-u", "-z")
    stages: dict[str, set[int]] = {}
    for record in result.stdout.split("\0"):
        if not record or "\t" not in record:
            continue
        metadata, path = record.split("\t", 1)
        fields = metadata.split()
        if len(fields) >= 3:
            stages.setdefault(path, set()).add(int(fields[2]))
    return stages


def resolve_unmerged_with_local(repo: Path) -> tuple[bool, str]:
    """Resolve with stage 3, the replayed/stashed local side in our operations."""
    for path, stages in unmerged_stages(repo).items():
        if 3 in stages:
            restored = git_run(repo, "checkout", "--theirs", "--", path)
            if restored.returncode != 0:
                return False, tail(restored.stderr or restored.stdout)
            staged = git_run(repo, "add", "--", path)
        else:
            staged = git_run(repo, "rm", "-f", "--ignore-unmatch", "--", path)
        if staged.returncode != 0:
            return False, tail(staged.stderr or staged.stdout)
    return not unmerged_stages(repo), "local side selected"


def llm_resolver() -> Path:
    explicit = os.environ.get("GIT_FLEET_LLM_RESOLVER") or os.environ.get("GIT_SYNC_LLM_RESOLVER")
    if explicit:
        return Path(explicit).expanduser()
    return Path(__file__).resolve().parent / "llm-resolve"


def resolve_unmerged(
    repo: Path,
    *,
    strategy: str,
    timeout: float,
) -> tuple[bool, str]:
    if not unmerged_stages(repo):
        return True, ""
    if strategy == "stop":
        return False, "automatic conflict resolution disabled"
    if strategy == "llm":
        resolver = llm_resolver()
        if resolver.is_file() and os.access(resolver, os.X_OK):
            try:
                result = subprocess.run(
                    [str(resolver), str(repo), "--json"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=max(1.0, timeout),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                llm_detail = f"LLM resolver timed out after {error.timeout:g}s"
            else:
                if result.returncode == 0 and not unmerged_stages(repo):
                    return True, "LLM conflict resolution"
                llm_detail = tail(result.stderr or result.stdout) or "LLM resolver left conflicts"
        else:
            llm_detail = f"LLM resolver unavailable: {resolver}"
        resolved, local_detail = resolve_unmerged_with_local(repo)
        if resolved:
            return True, f"{llm_detail}; local fallback"
        return False, f"{llm_detail}; {local_detail}"
    return resolve_unmerged_with_local(repo)


def snapshot_local_work(engine: Any, repo: Path) -> tuple[LocalSnapshot | None, str]:
    untracked = nul_paths(
        engine.output(repo, "ls-files", "--others", "--exclude-standard", "-z")
    )
    message = f"git-fleet local snapshot {engine.timestamp()}"
    stashed = engine.run(
        repo,
        "stash",
        "push",
        "--include-untracked",
        "--message",
        message,
    )
    if stashed.returncode != 0:
        return None, tail(stashed.stderr or stashed.stdout) or "could not snapshot local work"
    ref = engine.output(repo, "rev-parse", "refs/stash")
    if not ref or engine.output(repo, "status", "--porcelain"):
        return None, "local snapshot did not produce a clean worktree"
    return LocalSnapshot(ref=ref, untracked=untracked), ""


def restore_local_work(
    engine: Any,
    repo: Path,
    snapshot: LocalSnapshot,
    *,
    conflict_strategy: str,
    resolver_timeout: float,
) -> tuple[bool, str]:
    applied = engine.run(repo, "stash", "apply", snapshot.ref)
    resolved, resolution_detail = resolve_unmerged(
        repo,
        strategy=conflict_strategy,
        timeout=resolver_timeout,
    )
    if not resolved:
        return False, resolution_detail or tail(applied.stderr or applied.stdout)

    for path in snapshot.untracked:
        exists = engine.run(repo, "cat-file", "-e", f"{snapshot.ref}^3:{path}")
        if exists.returncode != 0:
            continue
        restored = engine.run(repo, "checkout", f"{snapshot.ref}^3", "--", path)
        if restored.returncode != 0:
            return False, tail(restored.stderr or restored.stdout)
        engine.run(repo, "reset", "-q", "HEAD", "--", path)

    if unmerged_stages(repo):
        return False, "conflicts remain after local snapshot restore"
    current_stash = engine.output(repo, "rev-parse", "refs/stash")
    if current_stash == snapshot.ref:
        dropped = engine.run(repo, "stash", "drop", "stash@{0}")
        if dropped.returncode != 0:
            return False, f"local work restored but snapshot retained: {snapshot.ref[:12]}"
    detail = "local work restored"
    if resolution_detail:
        detail += f" via {resolution_detail}"
    return True, detail


def abort_rebase(repo: Path) -> None:
    git_run(repo, "rebase", "--abort")


def rebase_local_commits(
    repo: Path,
    upstream: str,
    *,
    conflict_strategy: str,
    resolver_timeout: float,
) -> tuple[bool, str]:
    result = git_run(repo, "rebase", upstream)
    resolution_notes: list[str] = []
    attempts = 0
    while result.returncode != 0 and attempts < 100:
        attempts += 1
        if unmerged_stages(repo):
            resolved, detail = resolve_unmerged(
                repo,
                strategy=conflict_strategy,
                timeout=resolver_timeout,
            )
            if not resolved:
                return False, detail
            if detail:
                resolution_notes.append(detail)
            result = git_run(repo, "-c", "core.editor=true", "rebase", "--continue")
            continue
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "empty" in combined or "no changes" in combined:
            result = git_run(repo, "rebase", "--skip")
            continue
        break
    if result.returncode == 0:
        return True, "; ".join(dict.fromkeys(resolution_notes))
    return False, tail(result.stderr or result.stdout) or "rebase failed"
