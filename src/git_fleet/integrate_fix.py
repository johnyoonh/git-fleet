"""Integrate explicitly selected, tested repair PRs into the original source PR."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import tempfile

from publication_guard import github_slug, publication_block_reason
from verify_pr import canonical, DOMAIN, git, sha, snapshot, task_lock


def validate(receipt, source, repairs, repository, number, key):
    value = dict(receipt)
    signature = value.pop("signature", "")
    if not key or not isinstance(signature, str) or not hmac.compare_digest(
            signature, hmac.new(key.encode(), DOMAIN + canonical(value), hashlib.sha256).hexdigest()):
        raise ValueError("repair integration requires a trusted signed verification receipt")
    expected = {"repository": repository.lower(), "pr": number,
                "head_sha": source["head_sha"], "base_sha": source["base_sha"], "repair_shas": repairs}
    if any(value.get(k) != v for k, v in expected.items()) or value.get("status") != "passed":
        raise ValueError("repair receipt does not match current source and repair revisions")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["completed_at"])).total_seconds()
    if not 0 <= age <= 3600 or not value.get("checks") or any(c.get("returncode") != 0 for c in value["checks"]):
        raise ValueError("repair evidence expired or checks failed")
    return sha(value["merge_tree_sha"])


def compose(repo, worktree, source, repairs, expected_tree, number):
    git(repo, "worktree", "add", "--detach", str(worktree), source["head_sha"])
    for revision in [source["base_sha"], *repairs]:
        result = git(worktree, "-c", "user.name=git-fleet integration",
                     "-c", "user.email=git-fleet@example.invalid", "merge", "--no-ff",
                     "-m", f"Integrate tested repair into source PR #{number}", revision, check=False)
        if result.returncode:
            raise RuntimeError("repair integration conflict; isolated worktree preserved")
    if git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() != expected_tree:
        raise ValueError("integrated tree differs from the tested candidate")
    return git(worktree, "rev-parse", "HEAD").stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repair-pr", type=int, action="append", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.pr <= 0 or any(n <= 0 or n == args.pr for n in args.repair_pr):
        parser.error("source and repair PR numbers must be distinct positive integers")
    def gh(path):
        return json.loads(subprocess.run(["gh", "api", path], check=True, text=True,
                                         capture_output=True, timeout=30).stdout)
    remote = git(args.repo, "remote", "get-url", "origin").stdout.strip()
    repository = github_slug(remote)
    if not repository:
        parser.error("repair integration requires a github.com origin")
    source = snapshot(gh, repository, args.pr)
    if not source["head_ref"].startswith("chatgpt/"):
        parser.error("repair integration only targets the original chatgpt/ task branch")
    repairs = [snapshot(gh, repository, n)["head_sha"] for n in args.repair_pr]
    receipt = json.loads(args.receipt.read_text())
    expected_tree = validate(receipt, source, repairs, repository, args.pr,
                             os.getenv("GIT_FLEET_EVIDENCE_KEY", ""))
    blocked = publication_block_reason(remote, source["head_ref"], request=gh)
    if blocked:
        raise ValueError(blocked)
    root = Path.home() / ".local/state/git-fleet/verification"
    with task_lock(root, f"{repository}#{args.pr}"):
        for revision in [source["head_sha"], source["base_sha"], *repairs]:
            git(args.repo, "fetch", "--no-tags", "origin", revision)
        worktree = Path(tempfile.mkdtemp(prefix=f"integrate-{args.pr}-", dir=root))
        worktree.rmdir()
        new_head = compose(args.repo, worktree, source, repairs, expected_tree, args.pr)
        if (snapshot(gh, repository, args.pr) != source or
                [snapshot(gh, repository, n)["head_sha"] for n in args.repair_pr] != repairs):
            raise ValueError("source or repair moved before publication; reverify")
        # Ordinary fast-forward push only. Never update the default branch or use force.
        git(worktree, "push", "origin", f"HEAD:refs/heads/{source['head_ref']}")
        if snapshot(gh, repository, args.pr)["head_sha"] != new_head:
            raise ValueError("source moved after publication; current revision requires revalidation")
        print(json.dumps({"repository": repository, "pr": args.pr, "head_sha": new_head,
                          "repair_prs": args.repair_pr, "merge_tree_sha": expected_tree,
                          "next": "verify-pr and Jules certification for the new source head; no main merge authorized"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
