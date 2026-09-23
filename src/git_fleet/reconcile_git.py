#!/usr/bin/env python3
"""Low-level Git operations used by policy-driven repository reconciliation."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any


def tail(text: str, limit: int = 500) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[-limit:]


def git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"})
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def ref_exists(engine: Any, repo: Path, ref: str) -> bool:
    return engine.run(repo, "show-ref", "--verify", "--quiet", ref).returncode == 0


def ref_divergence(
    engine: Any,
    repo: Path,
    local_ref: str,
    remote_ref: str,
) -> tuple[int, int, str]:
    result = engine.run(
        repo,
        "rev-list",
        "--left-right",
        "--count",
        f"{local_ref}...{remote_ref}",
    )
    if result.returncode != 0:
        return 0, 0, tail(result.stderr or result.stdout) or "could not compare refs"
    try:
        raw_ahead, raw_behind = result.stdout.split(maxsplit=1)
        return int(raw_ahead), int(raw_behind), ""
    except (TypeError, ValueError):
        return 0, 0, "could not parse ref divergence"


def remote_default_branch(engine: Any, repo: Path) -> tuple[str, str]:
    discovered = engine.run(repo, "ls-remote", "--symref", "origin", "HEAD")
    if discovered.returncode == 0:
        match = re.search(
            r"^ref:\s+refs/heads/([^\s]+)\s+HEAD$",
            discovered.stdout,
            re.MULTILINE,
        )
        if match is not None:
            branch = match.group(1)
            cached = engine.run(repo, "remote", "set-head", "origin", branch)
            if cached.returncode != 0:
                return "", tail(cached.stderr or cached.stdout) or "could not cache origin HEAD"
            return branch, ""
    symbolic = engine.output(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
    )
    if symbolic.startswith("origin/") and len(symbolic) > len("origin/"):
        return symbolic.removeprefix("origin/"), ""
    if discovered.returncode != 0:
        return "", tail(discovered.stderr or discovered.stdout) or "could not query origin HEAD"
    return "", "origin does not advertise a default branch"


def backup_ref_name(engine: Any, branch: str, head: str) -> str:
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "head"
    stamp = re.sub(r"[^0-9A-Za-z]+", "-", engine.timestamp()).strip("-")
    return f"refs/git-fleet/backups/{safe_branch}/{stamp}-{head[:12]}"


def create_backup_ref(
    engine: Any,
    repo: Path,
    branch: str,
    head: str,
) -> tuple[str, str]:
    ref = backup_ref_name(engine, branch, head)
    updated = engine.run(repo, "update-ref", ref, head)
    if updated.returncode != 0:
        return "", tail(updated.stderr or updated.stdout)
    return ref, ""


def submodule_pointer_drift_is_safe(engine: Any, repo: Path) -> bool:
    modules = engine.run(
        repo,
        "config",
        "--file",
        ".gitmodules",
        "--get-regexp",
        r"^submodule\..*\.path$",
    )
    if modules.returncode != 0:
        return False
    submodule_paths = {
        line.split(None, 1)[1]
        for line in modules.stdout.splitlines()
        if len(line.split(None, 1)) == 2
    }
    status = engine.run(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
    )
    if status.returncode != 0 or not status.stdout:
        return False
    entries = [entry for entry in status.stdout.split("\0") if entry]
    for entry in entries:
        if len(entry) < 4:
            return False
        path = entry[3:]
        if path not in submodule_paths:
            return False
        subrepo = repo / path
        if not subrepo.is_dir() or engine.output(subrepo, "status", "--porcelain"):
            return False
    return bool(entries)


def switch_to_branch(
    engine: Any,
    repo: Path,
    *,
    branch: str,
    remote_ref: str,
) -> tuple[bool, str]:
    local_ref = f"refs/heads/{branch}"
    if ref_exists(engine, repo, local_ref):
        switched = engine.run(repo, "switch", "--quiet", branch)
    else:
        switched = engine.run(
            repo,
            "switch",
            "--quiet",
            "--track",
            "-c",
            branch,
            remote_ref,
        )
    if switched.returncode != 0:
        return False, tail(switched.stderr or switched.stdout) or f"could not switch to {branch}"
    tracked = engine.run(repo, "branch", "--set-upstream-to", remote_ref, branch)
    if tracked.returncode != 0:
        return False, tail(tracked.stderr or tracked.stdout) or "could not set branch upstream"
    return True, ""


def run_hook(
    repo: Path,
    *,
    policy: Any,
    branch: str,
    upstream: str,
    old_head: str,
    new_head: str,
    timeout: float,
) -> tuple[str, str]:
    hook = repo / ".repo-sync/post-sync"
    if not hook.exists():
        return "none", ""
    if not hook.is_file() or not os.access(hook, os.X_OK):
        return "failed", f"post-sync hook is not executable: {hook}"
    env = dict(os.environ)
    env.update(
        {
            "REPO_SYNC_HOOK": "1",
            "REPO_SYNC_REPO": str(repo),
            "REPO_SYNC_SLUG": str(policy.slug),
            "REPO_SYNC_BRANCH": branch,
            "REPO_SYNC_UPSTREAM": upstream,
            "REPO_SYNC_OLD_HEAD": old_head,
            "REPO_SYNC_NEW_HEAD": new_head,
        }
    )
    try:
        result = subprocess.run(
            [str(hook)],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1.0, timeout),
            check=False,
        )
    except OSError as error:
        return "failed", f"post-sync hook could not start: {error}"
    except subprocess.TimeoutExpired as error:
        return "failed", f"post-sync hook timed out after {error.timeout:g}s"
    output = tail(result.stderr or result.stdout)
    if result.returncode != 0:
        return "failed", f"post-sync hook exited {result.returncode}: {output or 'no output'}"
    return "ok", output
