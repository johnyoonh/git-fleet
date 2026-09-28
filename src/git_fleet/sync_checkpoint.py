"""Optional dirty-work checkpoint layer for repo sync.

Loaded into the existing sync command namespace after the retained implementation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


CHECKPOINT_DIRTY = os.environ.get("GIT_SYNC_CHECKPOINT_DIRTY", "").strip().lower() in {
    "1", "true", "yes", "on"
}
CHECKPOINT_MESSAGE = "chore(git-fleet): checkpoint local work"
_ORIGINAL_PARSER = globals().get("parser", lambda levels: argparse.ArgumentParser())
_ORIGINAL_LOAD_POLICY_AND_RUNNER = globals().get("load_policy_and_runner", lambda: (None, None))


class _CheckpointDirtyAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        global CHECKPOINT_DIRTY
        CHECKPOINT_DIRTY = True
        setattr(namespace, self.dest, True)


def parser(levels: tuple[str, ...]) -> argparse.ArgumentParser:
    result = _ORIGINAL_PARSER(levels)
    result.add_argument(
        "--checkpoint-dirty",
        nargs=0,
        action=_CheckpointDirtyAction,
        default=CHECKPOINT_DIRTY,
        help=(
            "on a dirty remote-default checkout, commit all current work through normal Git hooks "
            "before reconciliation so incoming commits and local changes share one rebase context"
        ),
    )
    return result


def _git_dir(engine: Any, repo: Path) -> Path | None:
    raw = engine.output(repo, "rev-parse", "--git-dir")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _git_operation_in_progress(engine: Any, repo: Path) -> str:
    gitdir = _git_dir(engine, repo)
    if gitdir is None:
        return "git directory unavailable"
    names = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-apply",
        "rebase-merge",
        "BISECT_LOG",
    )
    active = [name for name in names if (gitdir / name).exists()]
    return ", ".join(active)


def _restore_index(engine: Any, repo: Path, tree: str) -> None:
    if tree:
        engine.run(repo, "read-tree", tree)


def _checkpoint_dirty_work(
    engine: Any,
    runner: Any,
    candidate: Any,
    *,
    dry_run: bool,
) -> tuple[str, str]:
    """Return (checkpoint_sha, detail); detail beginning error: blocks sync."""
    if not CHECKPOINT_DIRTY:
        return "", ""
    policy = candidate.policy
    automation = candidate.automation
    repo = Path(policy.path).expanduser().resolve()
    if not repo.is_dir():
        return "", ""
    if automation.respect_leases and engine.live_aisess_leases(repo):
        return "", ""

    branch, _upstream, dirty, _ahead, _behind = engine.repo_status(repo)
    if not dirty:
        return "", ""
    default_branch, default_error = runner.remote_default_branch(engine, repo)
    if not default_branch:
        return "", f"error: cannot checkpoint dirty work: {default_error or 'remote default branch unavailable'}"
    if branch != default_branch:
        return "", f"skipped checkpoint: dirty checkout is on {branch}, not remote default {default_branch}"
    operation = _git_operation_in_progress(engine, repo)
    if operation:
        return "", f"error: cannot checkpoint dirty work during Git operation: {operation}"
    if dry_run:
        return "", "would checkpoint dirty default-branch work through normal Git hooks"

    # Serialize the checkpoint independently. sync_one will reacquire the same
    # repository lock and re-read state after this bounded mutation.
    with engine.repo_lock(repo) as acquired:
        if not acquired:
            return "", ""
        branch, _upstream, dirty, _ahead, _behind = engine.repo_status(repo)
        if not dirty or branch != default_branch:
            return "", ""
        original_index = engine.output(repo, "write-tree")
        if not original_index:
            return "", "error: could not snapshot the current Git index before checkpoint"
        staged = engine.run(repo, "add", "-A", "--")
        if staged.returncode != 0:
            _restore_index(engine, repo, original_index)
            return "", "error: checkpoint staging failed: " + (staged.stderr or staged.stdout or "git add failed").strip()[-500:]
        # Inspect WIP state contract if present
        wip_file = repo / ".local" / "wip-state.json"
        wip_data: dict[str, Any] | None = None
        if wip_file.is_file():
            try:
                wip_data = json.loads(wip_file.read_text(encoding="utf-8"))
            except Exception:
                wip_data = None

        if wip_data and wip_data.get("testsPassed") is False:
            return "", "error: checkpoint rejected: .local/wip-state.json reports tests failed"

        last_lease: dict[str, Any] | None = None
        if hasattr(engine, "last_lease_metadata"):
            last_lease = engine.last_lease_metadata(repo)

        session_id = None
        agent = None
        purpose = None
        if wip_data:
            session_id = wip_data.get("sessionId") or wip_data.get("session_id")
            agent = wip_data.get("agent") or wip_data.get("agentModel") or wip_data.get("model")
            purpose = wip_data.get("purpose")
        if not session_id and last_lease:
            session_id = last_lease.get("session_id")
        if not agent and last_lease:
            agent = last_lease.get("model") or last_lease.get("preset")

        trailers: list[str] = []
        if session_id:
            trailers.append(f"Session-ID: {session_id}")
        if agent:
            trailers.append(f"Agent: {agent}")
        if purpose:
            trailers.append(f"Wip-Purpose: {purpose}")
        if last_lease and last_lease.get("eviction_reason") == "stale_lease":
            trailers.append("Lease-Stale: true")

        commit_message = CHECKPOINT_MESSAGE
        if trailers:
            commit_message = f"{CHECKPOINT_MESSAGE}\n\n" + "\n".join(trailers)

        committed = engine.run(repo, "commit", "-m", commit_message)
        if committed.returncode != 0:
            _restore_index(engine, repo, original_index)
            return "", (
                "error: checkpoint commit rejected; original index restored: "
                + (committed.stderr or committed.stdout or "git commit failed").strip()[-500:]
            )
        sha = engine.output(repo, "rev-parse", "HEAD")
        detail = f"checkpointed dirty work as {sha[:12]}"
        if session_id:
            detail += f" (session {session_id})"
        return sha, detail


def load_policy_and_runner() -> tuple[Any, Any]:
    policy_module, runner = _ORIGINAL_LOAD_POLICY_AND_RUNNER()
    original_sync_one = runner.sync_one

    def checkpointing_sync_one(
        engine: Any,
        candidate: Any,
        *,
        dry_run: bool,
        hook_timeout: float,
        resolver_timeout: float,
    ):
        checkpoint_sha, checkpoint_detail = _checkpoint_dirty_work(
            engine,
            runner,
            candidate,
            dry_run=dry_run,
        )
        if checkpoint_detail.startswith("error:"):
            return runner.SyncResult(
                ts=engine.timestamp(),
                event="ERROR",
                repo=str(Path(candidate.policy.path).expanduser().resolve()),
                slug=str(candidate.policy.slug),
                detail=checkpoint_detail.removeprefix("error: "),
            )
        result = original_sync_one(
            engine,
            candidate,
            dry_run=dry_run,
            hook_timeout=hook_timeout,
            resolver_timeout=resolver_timeout,
        )
        if checkpoint_detail:
            result.detail = f"{checkpoint_detail}; {result.detail}" if result.detail else checkpoint_detail
        if checkpoint_sha and result.old_head == checkpoint_sha:
            # The original runner correctly records its own starting HEAD after
            # checkpointing. Keep the checkpoint detail as audit evidence.
            pass
        return result

    runner.sync_one = checkpointing_sync_one
    return policy_module, runner
