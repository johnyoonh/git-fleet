#!/usr/bin/env python3
"""Execute one policy-driven repository reconciliation candidate."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from reconcile_git import (
    create_backup_ref,
    ref_divergence,
    ref_exists,
    remote_default_branch,
    run_hook,
    submodule_pointer_drift_is_safe,
    switch_to_branch,
    tail,
)
from reconcile_worktree import (
    LocalSnapshot,
    abort_rebase,
    rebase_local_commits,
    restore_local_work,
    snapshot_local_work,
)


@dataclass
class SyncResult:
    ts: str
    event: str
    repo: str
    slug: str
    branch: str = ""
    upstream: str = ""
    old_head: str = ""
    new_head: str = ""
    hook_status: str = "none"
    detail: str = ""

    def json_record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Candidate:
    policy: Any
    automation: Any
    source: str


def choose_target(
    engine: Any,
    repo: Path,
    *,
    branch: str,
    current_upstream: str,
    automation: Any,
) -> tuple[str, str, str]:
    default_branch, default_error = remote_default_branch(engine, repo)
    if not default_branch:
        return "", "", default_error
    if automation.switch_default or branch == "HEAD":
        return default_branch, f"origin/{default_branch}", ""
    if current_upstream:
        return branch, current_upstream, ""
    candidate = f"origin/{branch}"
    if ref_exists(engine, repo, f"refs/remotes/{candidate}"):
        return branch, candidate, ""
    return "", "", f"current branch {branch} has no upstream"


def preflight_divergence(
    engine: Any,
    repo: Path,
    *,
    target_branch: str,
    target_remote: str,
) -> tuple[bool, int, int, str]:
    local_ref = f"refs/heads/{target_branch}"
    if not ref_exists(engine, repo, local_ref):
        return False, 0, 0, ""
    ahead, behind, error = ref_divergence(
        engine,
        repo,
        local_ref,
        f"refs/remotes/{target_remote}",
    )
    return True, ahead, behind, error


def restore_snapshot(
    engine: Any,
    repo: Path,
    snapshot: LocalSnapshot | None,
    *,
    automation: Any,
    resolver_timeout: float,
) -> tuple[bool, str]:
    if snapshot is None:
        return True, ""
    return restore_local_work(
        engine,
        repo,
        snapshot,
        conflict_strategy=automation.conflict_strategy,
        resolver_timeout=resolver_timeout,
    )


def persist_hook_intent(path: Path, old_head: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(old_head + "\n", encoding="utf-8")
    temporary.replace(path)


def sync_one(
    engine: Any,
    candidate: Candidate,
    *,
    dry_run: bool,
    hook_timeout: float,
    resolver_timeout: float,
) -> SyncResult:
    policy = candidate.policy
    automation = candidate.automation
    repo = Path(policy.path).expanduser().resolve()
    now = engine.timestamp()
    base = dict(ts=now, repo=str(repo), slug=str(policy.slug))
    if not repo.is_dir():
        return SyncResult(
            event="ERROR",
            detail="registered or discovered checkout is missing",
            **base,
        )

    remote_url = engine.output(repo, "config", "--get", "remote.origin.url")
    if not remote_url or engine.normalize_remote(remote_url) != policy.slug:
        return SyncResult(
            event="ERROR",
            detail="remote.origin.url does not match repository slug",
            **base,
        )

    leases = engine.live_aisess_leases(repo) if automation.respect_leases else []
    if leases:
        return SyncResult(
            event="LEASED",
            detail=", ".join(f"pid={item.get('pid')}" for item in leases),
            **base,
        )

    with engine.repo_lock(repo) as acquired:
        if not acquired:
            return SyncResult(
                event="LOCKED",
                detail="another sync process owns the repository lock",
                **base,
            )

        fetched = engine.run(repo, "fetch", "--quiet", "--prune")
        if fetched.returncode != 0:
            return SyncResult(
                event="ERROR",
                detail=tail(fetched.stderr) or "fetch failed",
                **base,
            )

        branch, current_upstream, dirty, _, _ = engine.repo_status(repo)
        old_head = engine.output(repo, "rev-parse", "HEAD")
        target_branch, target_remote, target_error = choose_target(
            engine,
            repo,
            branch=branch,
            current_upstream=current_upstream,
            automation=automation,
        )
        if not target_branch:
            return SyncResult(
                event="ERROR",
                branch=branch,
                upstream=current_upstream,
                old_head=old_head,
                detail=target_error,
                **base,
            )
        remote_full_ref = f"refs/remotes/{target_remote}"
        if not ref_exists(engine, repo, remote_full_ref):
            return SyncResult(
                event="ERROR",
                branch=branch,
                upstream=target_remote,
                old_head=old_head,
                detail=f"fetched target branch is missing: {target_remote}",
                **base,
            )

        allowed_submodule_drift = (
            dirty
            and automation.submodules
            and branch == target_branch
            and submodule_pointer_drift_is_safe(engine, repo)
        )
        if branch == "HEAD" and not automation.recover_detached:
            return SyncResult(
                event="SKIPPED",
                branch=branch,
                upstream=target_remote,
                old_head=old_head,
                detail="detached HEAD and recover_detached=false",
                **base,
            )

        local_exists, ahead, behind, compare_error = preflight_divergence(
            engine,
            repo,
            target_branch=target_branch,
            target_remote=target_remote,
        )
        if compare_error:
            return SyncResult(
                event="ERROR",
                branch=branch,
                upstream=target_remote,
                old_head=old_head,
                detail=compare_error,
                **base,
            )
        if ahead > 0 and behind > 0 and not automation.rebase_local:
            return SyncResult(
                event="SKIPPED",
                branch=branch,
                upstream=target_remote,
                old_head=old_head,
                detail=(
                    f"target branch is diverged (ahead={ahead} behind={behind}) "
                    "and rebase_local=false"
                ),
                **base,
            )

        needs_switch = branch != target_branch
        needs_clean_worktree = branch == "HEAD" or needs_switch or behind > 0
        snapshot_needed = (
            dirty and not allowed_submodule_drift and needs_clean_worktree
        )
        if snapshot_needed and not automation.snapshot_dirty:
            return SyncResult(
                event="SKIPPED",
                branch=branch,
                upstream=target_remote,
                old_head=old_head,
                detail="worktree is dirty and snapshot_dirty=false",
                **base,
            )

        actions: list[str] = []
        if snapshot_needed:
            actions.append("snapshot and restore dirty work")
        if branch == "HEAD":
            actions.append("back up detached HEAD")
        if needs_switch:
            actions.append(f"switch {branch} -> {target_branch}")
        if not local_exists:
            actions.append(f"create tracking branch {target_branch}")
        if ahead > 0 and behind > 0:
            actions.append(
                f"back up and rebase {ahead} local commit(s) "
                f"across {behind} remote commit(s)"
            )
        elif behind > 0:
            actions.append(f"fast-forward {behind} commit(s)")
        elif ahead > 0:
            actions.append(f"retain {ahead} local commit(s) ahead of remote")
        if not actions:
            actions.append("leave aligned worktree unchanged")
        if dry_run:
            return SyncResult(
                event="PLANNED",
                branch=branch,
                upstream=target_remote,
                old_head=old_head,
                detail=f"level={automation.level}; would " + "; ".join(actions),
                **base,
            )

        # Store retry intent in this worktree's Git metadata, never tracked files.
        git_dir = engine.output(repo, "rev-parse", "--absolute-git-dir")
        if not git_dir:
            return SyncResult(event="HOOK_ERROR", old_head=old_head,
                              new_head=old_head, hook_status="failed",
                              detail="cannot locate Git metadata for hook recovery", **base)
        pending_hook = Path(git_dir) / "repo-sync-pending-hook"
        retry_hook = pending_hook.exists()
        hook_old_head = old_head
        if retry_hook:
            try:
                hook_old_head = pending_hook.read_text(encoding="utf-8").strip() or old_head
            except OSError as error:
                return SyncResult(event="HOOK_ERROR", old_head=old_head,
                                  new_head=old_head, hook_status="failed",
                                  detail=f"cannot read pending hook: {error}", **base)
        if needs_clean_worktree:
            try:
                persist_hook_intent(pending_hook, hook_old_head)
            except OSError as error:
                return SyncResult(event="HOOK_ERROR", old_head=old_head,
                                  hook_status="failed",
                                  detail=f"cannot persist hook recovery state: {error}", **base)

        snapshot: LocalSnapshot | None = None
        if snapshot_needed:
            snapshot, snapshot_error = snapshot_local_work(engine, repo)
            if snapshot is None:
                return SyncResult(
                    event="ERROR",
                    branch=branch,
                    upstream=target_remote,
                    old_head=old_head,
                    detail=snapshot_error,
                    **base,
                )

        backup_refs: list[str] = []
        if branch == "HEAD":
            backup_ref, backup_error = create_backup_ref(
                engine,
                repo,
                "detached",
                old_head,
            )
            if not backup_ref:
                restore_snapshot(
                    engine,
                    repo,
                    snapshot,
                    automation=automation,
                    resolver_timeout=resolver_timeout,
                )
                return SyncResult(
                    event="ERROR",
                    old_head=old_head,
                    detail=backup_error,
                    **base,
                )
            backup_refs.append(backup_ref)

        if needs_switch:
            switched, switch_error = switch_to_branch(
                engine,
                repo,
                branch=target_branch,
                remote_ref=target_remote,
            )
            if not switched:
                restore_snapshot(
                    engine,
                    repo,
                    snapshot,
                    automation=automation,
                    resolver_timeout=resolver_timeout,
                )
                return SyncResult(
                    event="ERROR",
                    branch=branch,
                    upstream=target_remote,
                    old_head=old_head,
                    detail=f"target-branch switch failed: {switch_error}",
                    **base,
                )
        else:
            tracked = engine.run(
                repo,
                "branch",
                "--set-upstream-to",
                target_remote,
                target_branch,
            )
            if tracked.returncode != 0:
                restore_snapshot(
                    engine,
                    repo,
                    snapshot,
                    automation=automation,
                    resolver_timeout=resolver_timeout,
                )
                return SyncResult(
                    event="ERROR",
                    branch=target_branch,
                    upstream=target_remote,
                    old_head=old_head,
                    detail=(
                        tail(tracked.stderr or tracked.stdout)
                        or "could not set branch upstream"
                    ),
                    **base,
                )

        # Recompute after branch creation/switch in case the local target did not exist.
        _, ahead, behind, compare_error = preflight_divergence(
            engine,
            repo,
            target_branch=target_branch,
            target_remote=target_remote,
        )
        if compare_error:
            restore_snapshot(
                engine,
                repo,
                snapshot,
                automation=automation,
                resolver_timeout=resolver_timeout,
            )
            return SyncResult(
                event="ERROR",
                old_head=old_head,
                detail=compare_error,
                **base,
            )

        integration_detail = ""
        if ahead > 0 and behind > 0:
            backup_ref, backup_error = create_backup_ref(
                engine,
                repo,
                target_branch,
                engine.output(repo, "rev-parse", "HEAD"),
            )
            if not backup_ref:
                restore_snapshot(
                    engine,
                    repo,
                    snapshot,
                    automation=automation,
                    resolver_timeout=resolver_timeout,
                )
                return SyncResult(
                    event="ERROR",
                    old_head=old_head,
                    detail=backup_error,
                    **base,
                )
            backup_refs.append(backup_ref)
            integrated, integration_detail = rebase_local_commits(
                repo,
                target_remote,
                conflict_strategy=automation.conflict_strategy,
                resolver_timeout=resolver_timeout,
            )
            if not integrated:
                abort_rebase(repo)
                _, restore_detail = restore_snapshot(
                    engine,
                    repo,
                    snapshot,
                    automation=automation,
                    resolver_timeout=resolver_timeout,
                )
                detail = integration_detail or "rebase failed"
                if backup_refs:
                    detail += f"; recovery={','.join(backup_refs)}"
                if restore_detail:
                    detail += f"; {restore_detail}"
                return SyncResult(
                    event="ERROR",
                    branch=target_branch,
                    upstream=target_remote,
                    old_head=old_head,
                    detail=detail,
                    **base,
                )
        elif behind > 0:
            merged = engine.run(
                repo,
                "merge",
                "--ff-only",
                "--quiet",
                target_remote,
            )
            if merged.returncode != 0:
                _, restore_detail = restore_snapshot(
                    engine,
                    repo,
                    snapshot,
                    automation=automation,
                    resolver_timeout=resolver_timeout,
                )
                detail = tail(merged.stderr or merged.stdout) or "fast-forward failed"
                if restore_detail:
                    detail += f"; {restore_detail}"
                return SyncResult(
                    event="ERROR",
                    branch=target_branch,
                    upstream=target_remote,
                    old_head=old_head,
                    detail=detail,
                    **base,
                )

        restored, restore_detail = restore_snapshot(
            engine,
            repo,
            snapshot,
            automation=automation,
            resolver_timeout=resolver_timeout,
        )
        if not restored:
            assert snapshot is not None
            return SyncResult(
                event="ERROR",
                branch=target_branch,
                upstream=target_remote,
                old_head=old_head,
                new_head=engine.output(repo, "rev-parse", "HEAD"),
                detail=(
                    "repository integrated but local snapshot restore needs attention: "
                    f"{restore_detail}; snapshot={snapshot.ref[:12]}"
                ),
                **base,
            )

        new_head = engine.output(repo, "rev-parse", "HEAD")
        changed = needs_switch or new_head != old_head
        if not changed and not retry_hook:
            detail = f"level={automation.level}; already aligned"
            if ahead > 0:
                detail += f"; local branch remains ahead by {ahead}"
            if allowed_submodule_drift:
                detail += "; clean submodule pointer drift remains"
            if restore_detail:
                detail += f"; {restore_detail}"
            return SyncResult(
                event="RECONCILED" if snapshot else "UNCHANGED",
                branch=target_branch,
                upstream=target_remote,
                old_head=old_head,
                new_head=new_head,
                detail=detail,
                **base,
            )

        try:
            # Persist before executing so a timeout or interrupted runner retries later.
            if (repo / ".repo-sync/post-sync").exists() or retry_hook:
                persist_hook_intent(pending_hook, hook_old_head)
            hook_status, hook_detail = run_hook(
                repo,
                policy=policy,
                branch=target_branch,
                upstream=target_remote,
                old_head=hook_old_head,
                new_head=new_head,
                timeout=hook_timeout,
            )
            if hook_status != "failed":
                pending_hook.unlink(missing_ok=True)
        except OSError as error:
            hook_status, hook_detail = "failed", f"cannot persist hook recovery state: {error}"
        automated = bool(snapshot or backup_refs or (ahead > 0 and behind > 0))
        event = (
            "RECONCILED" if automated else "SYNCED"
        ) if hook_status != "failed" else "HOOK_ERROR"
        detail_parts = [f"level={automation.level}"]
        if retry_hook:
            detail_parts.append("retried pending post-sync hook")
        if needs_switch:
            detail_parts.append(f"switched {branch} -> {target_branch}")
        if ahead > 0 and behind > 0:
            detail_parts.append(
                f"rebased {ahead} local commit(s) across {behind} remote commit(s)"
            )
        elif behind > 0:
            detail_parts.append(f"fast-forwarded {behind} commit(s)")
        if integration_detail:
            detail_parts.append(integration_detail)
        if restore_detail:
            detail_parts.append(restore_detail)
        if backup_refs:
            detail_parts.append(f"recovery={','.join(backup_refs)}")
        if hook_detail:
            detail_parts.append(f"hook: {hook_detail}")
        detail_parts.append(f"{old_head[:12]}..{new_head[:12]}")
        return SyncResult(
            event=event,
            branch=target_branch,
            upstream=target_remote,
            old_head=old_head,
            new_head=new_head,
            hook_status=hook_status,
            detail="; ".join(detail_parts),
            **base,
        )
