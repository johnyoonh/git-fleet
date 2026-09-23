"""Automatic non-force publication layer for repo sync.

Loaded after the retained sync implementation and dirty-work checkpoint layer.
Every repository that is eligible for worktree reconciliation also publishes
committed default-branch work when it is safely ahead of origin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_PUBLISH_ORIGINAL_LOAD_POLICY_AND_RUNNER = load_policy_and_runner
_BLOCKED_EVENTS = {"ERROR", "HOOK_ERROR", "SKIPPED", "LEASED", "LOCKED"}


def _append_detail(result: Any, detail: str) -> None:
    result.detail = f"{result.detail}; {detail}" if result.detail else detail


def _remote_branch(upstream: str) -> str:
    if not upstream.startswith("origin/"):
        return ""
    return upstream.split("/", 1)[1]


def _replace_retained_ahead_detail(result: Any, ahead: int, replacement: str) -> None:
    retained = f"retain {ahead} local commit(s) ahead of remote"
    remains = f"; local branch remains ahead by {ahead}"
    if retained in result.detail:
        result.detail = result.detail.replace(retained, replacement)
    else:
        result.detail = result.detail.replace(remains, "")
        _append_detail(result, replacement)


def _publish_after_sync(
    engine: Any,
    runner: Any,
    candidate: Any,
    result: Any,
    *,
    dry_run: bool,
) -> Any:
    if result.event in _BLOCKED_EVENTS or not result.upstream:
        return result

    target_branch = _remote_branch(result.upstream)
    if not target_branch:
        _append_detail(
            result,
            f"publication skipped: upstream {result.upstream} is not origin",
        )
        return result

    repo = Path(candidate.policy.path).expanduser().resolve()
    local_exists, ahead, behind, compare_error = runner.preflight_divergence(
        engine,
        repo,
        target_branch=target_branch,
        target_remote=result.upstream,
    )
    if compare_error:
        result.event = "ERROR"
        _append_detail(result, f"publication preflight failed: {compare_error}")
        return result
    if not local_exists or ahead <= 0:
        return result

    if dry_run:
        if behind > 0:
            _append_detail(
                result,
                f"would publish {ahead} rebased local commit(s) to {result.upstream} after reconciliation",
            )
        else:
            _replace_retained_ahead_detail(
                result,
                ahead,
                f"publish {ahead} local commit(s) to {result.upstream}",
            )
        return result

    automation = candidate.automation
    if automation.respect_leases and engine.live_aisess_leases(repo):
        _append_detail(result, "publication deferred: active external lease")
        return result

    with engine.repo_lock(repo) as acquired:
        if not acquired:
            _append_detail(result, "publication deferred: repository lock is busy")
            return result

        current_branch = engine.output(repo, "branch", "--show-current") or "HEAD"
        if current_branch != target_branch:
            _append_detail(
                result,
                f"publication deferred: checkout moved to {current_branch}",
            )
            return result

        local_exists, ahead, behind, compare_error = runner.preflight_divergence(
            engine,
            repo,
            target_branch=target_branch,
            target_remote=result.upstream,
        )
        if compare_error:
            result.event = "ERROR"
            _append_detail(result, f"publication preflight failed: {compare_error}")
            return result
        if not local_exists or ahead <= 0:
            return result
        if behind > 0:
            _append_detail(
                result,
                f"publication deferred: local branch is behind {result.upstream} by {behind}",
            )
            return result

        pushed = engine.run(
            repo,
            "push",
            "--porcelain",
            "origin",
            f"refs/heads/{target_branch}:refs/heads/{target_branch}",
        )
        if pushed.returncode != 0:
            result.event = "ERROR"
            detail = runner.tail(pushed.stderr or pushed.stdout) or "git push failed"
            _append_detail(result, f"publication failed: {detail}")
            return result

    _replace_retained_ahead_detail(
        result,
        ahead,
        f"published {ahead} local commit(s) to {result.upstream}",
    )
    if result.event == "UNCHANGED":
        result.event = "PUBLISHED"
    return result


def load_policy_and_runner() -> tuple[Any, Any]:
    policy_module, runner = _PUBLISH_ORIGINAL_LOAD_POLICY_AND_RUNNER()
    original_sync_one = runner.sync_one

    def publishing_sync_one(
        engine: Any,
        candidate: Any,
        *,
        dry_run: bool,
        hook_timeout: float,
        resolver_timeout: float,
    ):
        result = original_sync_one(
            engine,
            candidate,
            dry_run=dry_run,
            hook_timeout=hook_timeout,
            resolver_timeout=resolver_timeout,
        )
        return _publish_after_sync(
            engine,
            runner,
            candidate,
            result,
            dry_run=dry_run,
        )

    runner.sync_one = publishing_sync_one
    return policy_module, runner
