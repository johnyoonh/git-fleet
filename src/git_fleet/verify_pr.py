"""Explicit, isolated PR integration testing and revision-bound evidence receipts.

A Git worktree is isolation from accidental checkout edits, NOT a security sandbox.
Run untrusted code only on a disposable, credential-isolated worker.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

SHA = re.compile(r"[0-9a-f]{40}")
DOMAIN = b"git-fleet/evidence/v1\0"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def git(repo, *args, check=True, timeout=120):
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
                             *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr[-1500:]}")
    return result


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("expected a full 40-character commit SHA")
    return value


def load_checks(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError("checks must be a nonempty JSON array (at most 32 commands)")
    for row in value:
        if (not isinstance(row, list) or not row or
                any(not isinstance(x, str) or not x or "\0" in x for x in row)):
            raise ValueError("each check must be a nonempty argv array, never a shell string")
    return value


def test_environment(home):
    # Allowlist instead of attempting to enumerate every cloud/provider token.
    allowed = {"PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP",
               "LANG", "LC_ALL", "TZ"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(HOME=str(home), USERPROFILE=str(home), CI="true",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="false",
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="credential.helper",
               GIT_CONFIG_VALUE_0="")
    return env


@contextmanager
def task_lock(root, key):
    root.mkdir(parents=True, exist_ok=True)
    path = root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise RuntimeError("PR already leased; reconcile abandoned locks explicitly") from exc
    try:
        (path / "owner.json").write_text(json.dumps({"pid": os.getpid(), "task": key}))
        yield
    finally:
        (path / "owner.json").unlink(missing_ok=True)
        path.rmdir()


def snapshot(gh, repository, pr):
    value = gh(f"repos/{repository}/pulls/{pr}")
    if value.get("state") != "open" or value.get("draft"):
        raise ValueError("verification requires an open, ready PR")
    if value["head"]["repo"]["full_name"].lower() != repository.lower():
        raise ValueError("fork PRs require a separate untrusted-code executor")
    return {"head_sha": sha(value["head"]["sha"]),
            "base_sha": sha(value["base"]["sha"]),
            "head_ref": value["head"]["ref"], "base_ref": value["base"]["ref"]}


def verify(repo, repository, pr, revisions, checks, output, *, root=None,
           timeout=600, signing_key="", reread=None, include=None):
    """Test an exact base + head (+ explicitly selected repair commits) in a worktree.

    Caller is responsible for authenticating the PR snapshot and authorizing code
    execution. Neither a receipt nor a successful exit authorizes merge/deployment.
    """
    repo, output = Path(repo).resolve(), Path(output).resolve()
    base, head = sha(revisions["base_sha"]), sha(revisions["head_sha"])
    fixes = [sha(x) for x in (include or [])]
    if not checks or timeout <= 0:
        raise ValueError("nonempty checks and a positive timeout are required")
    root = Path(root or Path.home() / ".local/state/git-fleet/verification").resolve()
    with task_lock(root, f"{repository}#{pr}"):
        worktree = Path(tempfile.mkdtemp(prefix=f"pr-{pr}-", dir=root))
        worktree.rmdir()
        added = False
        receipt = {"schema_version": 1, "repository": repository.lower(), "pr": pr,
                   "head_sha": head, "base_sha": base, "repair_shas": fixes,
                   "merge_tree_sha": "", "status": "failed", "checks": [],
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "platform": platform.system(), "worktree": str(worktree),
                   "checks_digest": hashlib.sha256(canonical(checks)).hexdigest(),
                   "merge_authorization": "none"}
        try:
            git(repo, "worktree", "add", "--detach", str(worktree), base)
            added = True
            # Build the actual proposed integration, not merely the PR's head.
            for commit in [head, *fixes]:
                result = git(worktree, "-c", "user.name=git-fleet verifier",
                             "-c", "user.email=git-fleet@example.invalid", "merge",
                             "--no-ff", "--no-edit", commit, check=False)
                if result.returncode:
                    raise RuntimeError("candidate integration conflicts; worktree preserved")
            tree = git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
            receipt["merge_tree_sha"] = sha(tree)
            with tempfile.TemporaryDirectory(prefix="fleet-test-home-") as home:
                env = test_environment(home)
                for command in checks:
                    start = time.monotonic()
                    with tempfile.TemporaryFile() as log:
                        try:
                            proc = subprocess.run(command, cwd=worktree, env=env,
                                                  stdout=log, stderr=subprocess.STDOUT,
                                                  timeout=timeout, check=False)
                            code, timed_out = proc.returncode, False
                        except subprocess.TimeoutExpired:
                            code, timed_out = 124, True
                        log.seek(0)
                        digest = hashlib.file_digest(log, "sha256").hexdigest()
                    receipt["checks"].append({"argv": command, "returncode": code,
                                              "timed_out": timed_out, "log_sha256": digest,
                                              "seconds": round(time.monotonic() - start, 3)})
                    if code:
                        raise RuntimeError("candidate check failed; worktree preserved")
            if git(worktree, "status", "--porcelain", "--untracked-files=no").stdout.strip():
                raise RuntimeError("checks changed tracked content; evidence is invalid")
            if git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() != tree:
                raise RuntimeError("checks changed the integration tree")
            if reread and reread() != revisions:
                raise RuntimeError("PR head or base changed during verification")
            receipt["status"] = "passed"
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            receipt["error"] = str(exc)
        finally:
            receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
            if signing_key:
                receipt["signature"] = hmac.new(signing_key.encode(), DOMAIN + canonical(receipt),
                                                 hashlib.sha256).hexdigest()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.name + f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(receipt, indent=2) + "\n")
            temporary.replace(output)
            if added and receipt["status"] == "passed":
                # No --force: preserve unexpected/untracked test outputs rather than delete them.
                git(repo, "worktree", "remove", str(worktree), check=False)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--checks", type=Path, required=True,
                        help="trusted local JSON file of argv arrays, not PR-controlled instructions")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--trust-code", action="store_true",
                        help="explicit authorization to execute this PR's code on this machine")
    parser.add_argument("--include-pr", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    if not args.trust_code or args.pr <= 0:
        parser.error("positive --pr and --trust-code are required; a worktree is not a sandbox")
    from publication_guard import github_slug
    def gh(path):
        return json.loads(subprocess.run(["gh", "api", path], check=True, text=True,
                                         capture_output=True, timeout=30).stdout)
    remote = git(args.repo, "remote", "get-url", "origin").stdout.strip()
    repository = github_slug(remote)
    if not repository:
        parser.error("verify-pr requires a github.com origin")
    revisions = snapshot(gh, repository, args.pr)
    commits = [revisions["base_sha"], revisions["head_sha"]]
    fixes = []
    for number in args.include_pr:
        fix = snapshot(gh, repository, number)
        fixes.append(fix["head_sha"])
    for commit in [*commits, *fixes]:
        git(args.repo, "fetch", "--no-tags", "origin", commit)
    receipt = verify(args.repo, repository, args.pr, revisions, load_checks(args.checks),
                     args.output, timeout=args.timeout,
                     signing_key=os.getenv("GIT_FLEET_EVIDENCE_KEY", ""), include=fixes,
                     reread=lambda: snapshot(gh, repository, args.pr))
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
