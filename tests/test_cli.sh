#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/git-fleet-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

python3 -m py_compile "$ROOT"/src/git_fleet/*.py
"$ROOT/bin/git-fleet" --help | grep -q 'Usage: git-fleet'

HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/config" XDG_STATE_HOME="$TMP/state" \
  "$ROOT/bin/git-fleet" policy show --json > "$TMP/policy.json"
python3 - "$TMP/policy.json" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["level"] == "safe", data
assert data["defaults"]["mutate"] is True
assert data["defaults"]["rebase_local"] is False
assert data["defaults"]["publish"] is False
PY

python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]) / "src" / "git_fleet"))
import automation_policy

assert all(profile.publish is False for profile in automation_policy.PROFILES.values())
effective = automation_policy.effective_policy(
    {"level": "full", "repositories": {"example/repo": {"publish": False}}},
    slug="example/repo",
)
assert effective.publish is False
PY

PUBLISH_ORIGIN="$TMP/publish-origin.git"
PUBLISH_SEED="$TMP/publish-seed"
PUBLISH_CLONE="$TMP/publish-clone"
mkdir -p "$PUBLISH_SEED"
git init -q --bare "$PUBLISH_ORIGIN"
git init -q -b main "$PUBLISH_SEED"
git -C "$PUBLISH_SEED" config user.email "git-fleet-test@example.invalid"
git -C "$PUBLISH_SEED" config user.name "git-fleet test"
printf 'initial\n' > "$PUBLISH_SEED/file.txt"
git -C "$PUBLISH_SEED" add file.txt
git -C "$PUBLISH_SEED" commit -qm initial
git -C "$PUBLISH_SEED" remote add origin "$PUBLISH_ORIGIN"
git -C "$PUBLISH_SEED" push -q -u origin main
git --git-dir="$PUBLISH_ORIGIN" symbolic-ref HEAD refs/heads/main
git clone -q "$PUBLISH_ORIGIN" "$PUBLISH_CLONE"
git -C "$PUBLISH_CLONE" config user.email "git-fleet-test@example.invalid"
git -C "$PUBLISH_CLONE" config user.name "git-fleet test"
printf 'publish-default\n' > "$PUBLISH_CLONE/default.txt"
git -C "$PUBLISH_CLONE" add default.txt
git -C "$PUBLISH_CLONE" commit -qm publish-default
PUBLISH_HEAD=$(git -C "$PUBLISH_CLONE" rev-parse HEAD)
PUBLISH_SLUG="${PUBLISH_ORIGIN%.git}"
printf '%s\t%s\tauto\tignore\n' "$PUBLISH_SLUG" "$PUBLISH_CLONE" > "$TMP/publish-registry.tsv"
cat > "$TMP/publish-policy.toml" <<'TOML'
version = 1
level = "full"
TOML

run_publish_sync() {
  HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/config" XDG_STATE_HOME="$TMP/state" \
    GIT_FLEET_REGISTRY="$TMP/publish-registry.tsv" \
    GIT_FLEET_DYNAMIC_REGISTRY="$TMP/discovered.tsv" \
    GIT_FLEET_PR_WATCHLIST="$TMP/pr-watches.tsv" \
    GIT_FLEET_POLICY="$TMP/publish-policy.toml" \
    GIT_FLEET_POLICY_STATE="$TMP/publish-state.toml" \
    GIT_FLEET_STATE_DIR="$TMP/repo-sync-state" \
    "$ROOT/bin/git-fleet" sync --registry-only --repo "$PUBLISH_CLONE" --json "$@"
}

PUBLISH_ORIGIN_INITIAL=$(git --git-dir="$PUBLISH_ORIGIN" rev-parse refs/heads/main)
if ! run_publish_sync > "$TMP/publish-default.jsonl"; then
  cat "$TMP/publish-default.jsonl" >&2
  exit 1
fi
grep -q 'publication disabled by policy' "$TMP/publish-default.jsonl"
! grep -q '"event": "PUBLISHED"' "$TMP/publish-default.jsonl"
[[ "$(git --git-dir="$PUBLISH_ORIGIN" rev-parse refs/heads/main)" == "$PUBLISH_ORIGIN_INITIAL" ]]
[[ "$(git -C "$PUBLISH_CLONE" rev-parse HEAD)" == "$PUBLISH_HEAD" ]]

if ! run_publish_sync --set publish=true > "$TMP/publish-opt-in.jsonl"; then
  cat "$TMP/publish-opt-in.jsonl" >&2
  exit 1
fi
grep -q '"event": "PUBLISHED"' "$TMP/publish-opt-in.jsonl"
[[ "$(git --git-dir="$PUBLISH_ORIGIN" rev-parse refs/heads/main)" == "$PUBLISH_HEAD" ]]

printf 'publish-disabled\n' > "$PUBLISH_CLONE/disabled.txt"
git -C "$PUBLISH_CLONE" add disabled.txt
git -C "$PUBLISH_CLONE" commit -qm publish-disabled
DISABLED_HEAD=$(git -C "$PUBLISH_CLONE" rev-parse HEAD)
PUBLISH_REMOTE_BEFORE=$(git --git-dir="$PUBLISH_ORIGIN" rev-parse refs/heads/main)
if ! run_publish_sync --set publish=false > "$TMP/publish-disabled.jsonl"; then
  cat "$TMP/publish-disabled.jsonl" >&2
  exit 1
fi
grep -q 'publication disabled by policy' "$TMP/publish-disabled.jsonl"
! grep -q '"event": "PUBLISHED"' "$TMP/publish-disabled.jsonl"
[[ "$(git --git-dir="$PUBLISH_ORIGIN" rev-parse refs/heads/main)" == "$PUBLISH_REMOTE_BEFORE" ]]
[[ "$(git -C "$PUBLISH_CLONE" rev-parse HEAD)" == "$DISABLED_HEAD" ]]

SUBMODULE_ORIGIN="$TMP/submodule-origin.git"
SUBMODULE_SEED="$TMP/submodule-seed"
PARENT_ORIGIN="$TMP/parent-origin.git"
PARENT_SEED="$TMP/parent-seed"
PARENT_CLONE="$TMP/parent-clone"
mkdir -p "$SUBMODULE_SEED" "$PARENT_SEED"
git init -q --bare "$SUBMODULE_ORIGIN"
git init -q -b main "$SUBMODULE_SEED"
git -C "$SUBMODULE_SEED" config user.email "git-fleet-test@example.invalid"
git -C "$SUBMODULE_SEED" config user.name "git-fleet test"
printf 'submodule-v1\n' > "$SUBMODULE_SEED/file.txt"
git -C "$SUBMODULE_SEED" add file.txt
git -C "$SUBMODULE_SEED" commit -qm submodule-v1
git -C "$SUBMODULE_SEED" remote add origin "$SUBMODULE_ORIGIN"
git -C "$SUBMODULE_SEED" push -q -u origin main
git --git-dir="$SUBMODULE_ORIGIN" symbolic-ref HEAD refs/heads/main
SUBMODULE_V1=$(git -C "$SUBMODULE_SEED" rev-parse HEAD)

git init -q --bare "$PARENT_ORIGIN"
git init -q -b main "$PARENT_SEED"
git -C "$PARENT_SEED" config user.email "git-fleet-test@example.invalid"
git -C "$PARENT_SEED" config user.name "git-fleet test"
git -C "$PARENT_SEED" -c protocol.file.allow=always submodule add -q "$SUBMODULE_ORIGIN" dependency
git -C "$PARENT_SEED/dependency" checkout -q --detach "$SUBMODULE_V1"
git -C "$PARENT_SEED" add .gitmodules dependency
git -C "$PARENT_SEED" commit -qm parent-v1
git -C "$PARENT_SEED" remote add origin "$PARENT_ORIGIN"
git -C "$PARENT_SEED" push -q -u origin main
git --git-dir="$PARENT_ORIGIN" symbolic-ref HEAD refs/heads/main
git -C "$PARENT_SEED" branch feature
git -C "$PARENT_SEED" push -q origin feature

printf 'submodule-v2\n' > "$SUBMODULE_SEED/file.txt"
git -C "$SUBMODULE_SEED" add file.txt
git -C "$SUBMODULE_SEED" commit -qm submodule-v2
git -C "$SUBMODULE_SEED" push -q origin main
SUBMODULE_V2=$(git -C "$SUBMODULE_SEED" rev-parse HEAD)
git -C "$PARENT_SEED/dependency" fetch -q origin main
git -C "$PARENT_SEED/dependency" checkout -q --detach "$SUBMODULE_V2"
printf 'submodule-v3\n' > "$SUBMODULE_SEED/file.txt"
git -C "$SUBMODULE_SEED" add file.txt
git -C "$SUBMODULE_SEED" commit -qm submodule-v3
git -C "$SUBMODULE_SEED" push -q origin main
SUBMODULE_V3=$(git -C "$SUBMODULE_SEED" rev-parse HEAD)
git -C "$PARENT_SEED/dependency" fetch -q origin main
git -C "$PARENT_SEED/dependency" checkout -q --detach "$SUBMODULE_V3"
git -C "$PARENT_SEED" add dependency
git -C "$PARENT_SEED" commit -qm parent-v2
git -C "$PARENT_SEED" push -q origin main

git -c protocol.file.allow=always clone -q --branch feature "$PARENT_ORIGIN" "$PARENT_CLONE"
git -C "$PARENT_CLONE" -c protocol.file.allow=always submodule update --init --quiet
git -C "$PARENT_CLONE/dependency" switch -q -c child-local "$SUBMODULE_V2"
[[ -n "$(git -C "$PARENT_CLONE" status --porcelain)" ]]
PARENT_SLUG="${PARENT_ORIGIN%.git}"
printf '%s\t%s\tauto-submodules\tignore\n' "$PARENT_SLUG" "$PARENT_CLONE" > "$TMP/submodule-registry.tsv"
cat > "$TMP/submodule-policy.toml" <<'TOML'
version = 1
level = "full"
TOML

if ! HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/config" XDG_STATE_HOME="$TMP/state" \
  GIT_FLEET_REGISTRY="$TMP/submodule-registry.tsv" \
  GIT_FLEET_DYNAMIC_REGISTRY="$TMP/submodule-discovered.tsv" \
  GIT_FLEET_POLICY="$TMP/submodule-policy.toml" \
  GIT_FLEET_POLICY_STATE="$TMP/submodule-state.toml" \
  GIT_FLEET_STATE_DIR="$TMP/submodule-state" \
  "$ROOT/bin/git-fleet" sync --registry-only --repo "$PARENT_CLONE" --set submodules=true --json > "$TMP/submodule-sync.jsonl"; then
  cat "$TMP/submodule-sync.jsonl" >&2
  exit 1
fi
if ! grep -q '"event": "SYNCED"' "$TMP/submodule-sync.jsonl"; then
  cat "$TMP/submodule-sync.jsonl" >&2
  exit 1
fi
[[ "$(git -C "$PARENT_CLONE" branch --show-current)" == main ]]
[[ "$(git -C "$PARENT_CLONE" rev-parse HEAD)" == "$(git --git-dir="$PARENT_ORIGIN" rev-parse refs/heads/main)" ]]
[[ "$(git -C "$PARENT_CLONE/dependency" rev-parse HEAD)" == "$SUBMODULE_V2" ]]
[[ "$(git -C "$PARENT_CLONE/dependency" branch --show-current)" == child-local ]]
[[ -n "$(git -C "$PARENT_CLONE" status --porcelain)" ]]
if ! HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/config" XDG_STATE_HOME="$TMP/state" \
  GIT_FLEET_REGISTRY="$TMP/submodule-registry.tsv" \
  GIT_FLEET_DYNAMIC_REGISTRY="$TMP/submodule-discovered.tsv" \
  GIT_FLEET_POLICY="$TMP/submodule-policy.toml" \
  GIT_FLEET_POLICY_STATE="$TMP/submodule-state.toml" \
  GIT_FLEET_STATE_DIR="$TMP/submodule-state" \
  "$ROOT/bin/git-fleet" sync --registry-only --repo "$PARENT_CLONE" --set submodules=true --json > "$TMP/submodule-repeat.jsonl"; then
  cat "$TMP/submodule-repeat.jsonl" >&2
  exit 1
fi
if ! grep -q 'clean submodule pointer drift remains' "$TMP/submodule-repeat.jsonl"; then
  cat "$TMP/submodule-repeat.jsonl" >&2
  exit 1
fi
[[ "$(git -C "$PARENT_CLONE/dependency" rev-parse HEAD)" == "$SUBMODULE_V2" ]]

DISCOVERY_ROOT="$TMP/discovery"
PRIMARY="$TMP/primary"
ORDINARY="$DISCOVERY_ROOT/ordinary"
LINKED="$DISCOVERY_ROOT/task"
mkdir -p "$DISCOVERY_ROOT"

git init -q "$PRIMARY"
git -C "$PRIMARY" config user.email "git-fleet-test@example.invalid"
git -C "$PRIMARY" config user.name "git-fleet test"
git -C "$PRIMARY" checkout -q -b main
printf 'primary\n' > "$PRIMARY/README.md"
git -C "$PRIMARY" add README.md
git -C "$PRIMARY" commit -qm "init"

git clone -q "$PRIMARY" "$ORDINARY"
git -C "$PRIMARY" worktree add -q -b agent/test "$LINKED"

python3 - "$ROOT" "$DISCOVERY_ROOT" "$ORDINARY" "$LINKED" <<'PY'
from pathlib import Path
import sys

module_root = Path(sys.argv[1]) / "src" / "git_fleet"
sys.path.insert(0, str(module_root))
import repo_sync

root = Path(sys.argv[2]).resolve()
ordinary = Path(sys.argv[3]).resolve()
linked = Path(sys.argv[4]).resolve()
for cached_repo in (
    root / ".build" / "checkouts" / "CachedSwiftPackage",
    root / "build" / "DerivedData" / "SourcePackages" / "checkouts" / "CachedSwiftPackage",
):
    (cached_repo / ".git").mkdir(parents=True)
found = set(repo_sync.discover_repositories([root]))
assert ordinary in found, found
assert linked not in found, found
assert repo_sync.discover_repositories([linked]) == []
assert not any(str(repo).startswith(str(root / ".build")) for repo in found), found
assert not any("/DerivedData/" in str(repo) for repo in found), found
PY

python3 - "$ROOT" "$TMP" <<'PY'
from pathlib import Path
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

module_root = Path(sys.argv[1]) / "src" / "git_fleet"
sys.path.insert(0, str(module_root))
import repo_sync
import sync_checkpoint

tmp = Path(sys.argv[2])
os.environ["AISESS_STATE_DIR"] = str(tmp / "aisess")
os.environ["GIT_FLEET_STATE_DIR"] = str(tmp / "git-fleet-state")

fake_repo = tmp / "fake-lease-repo"
fake_repo.mkdir(parents=True, exist_ok=True)

# 1. Lease staleness test
now = datetime.now(timezone.utc).isoformat()
lease_dir = tmp / "aisess" / "repo-leases" / repo_sync.repo_key(fake_repo)
lease_dir.mkdir(parents=True, exist_ok=True)
lease_file = lease_dir / f"{os.getpid()}.json"
old = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
lease_file.write_text(json.dumps({
    "pid": os.getpid(),
    "session_id": "sess-stale-test",
    "model": "gpt-5-turbo",
    "started_at": old,
    "heartbeat_at": old,
}))

leases = repo_sync.live_aisess_leases(fake_repo, max_idle=900)
assert len(leases) == 0, leases  # Evicted!
assert not lease_file.exists()
last = repo_sync.last_lease_metadata(fake_repo)
assert last is not None
assert last["session_id"] == "sess-stale-test"
assert last["eviction_reason"] == "stale_lease"

# 2. Checkpoint WIP quality gate & session trailers
check_repo = tmp / "check-wip-repo"
check_repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init", "-q", "-b", "main", str(check_repo)], check=True)
subprocess.run(["git", "-C", str(check_repo), "config", "user.email", "test@example.invalid"], check=True)
subprocess.run(["git", "-C", str(check_repo), "config", "user.name", "test user"], check=True)
(check_repo / "doc.txt").write_text("v1\n")
subprocess.run(["git", "-C", str(check_repo), "add", "doc.txt"], check=True)
subprocess.run(["git", "-C", str(check_repo), "commit", "-qm", "init"], check=True)

(check_repo / "doc.txt").write_text("v2\n")
wip_dir = check_repo / ".local"
wip_dir.mkdir(parents=True, exist_ok=True)
wip_file = wip_dir / "wip-state.json"
wip_file.write_text(json.dumps({
    "schemaVersion": 1,
    "sessionId": "sess-abc-789",
    "agent": "codex-cli",
    "purpose": "verify session trailer checkpointing",
    "testsPassed": False,
}))

class MockCandidate:
    class Policy:
        path = str(check_repo)
        slug = "test/check-wip-repo"
    class Automation:
        respect_leases = True
    policy = Policy()
    automation = Automation()

class MockRunner:
    def remote_default_branch(self, engine, r):
        return "main", ""

sync_checkpoint.CHECKPOINT_DIRTY = True
sha, detail = sync_checkpoint._checkpoint_dirty_work(
    repo_sync,
    MockRunner(),
    MockCandidate(),
    dry_run=False,
)
assert sha == "", f"Expected empty sha, got {sha}"
assert "reports tests failed" in detail, detail

wip_file.write_text(json.dumps({
    "schemaVersion": 1,
    "sessionId": "sess-abc-789",
    "agent": "codex-cli",
    "purpose": "verify session trailer checkpointing",
    "testsPassed": True,
}))

sha, detail = sync_checkpoint._checkpoint_dirty_work(
    repo_sync,
    MockRunner(),
    MockCandidate(),
    dry_run=False,
)
assert sha != "", "Expected commit sha"
assert "sess-abc-789" in detail, detail
commit_body = subprocess.check_output(["git", "-C", str(check_repo), "log", "-1", "--format=%B"], text=True)
assert "Session-ID: sess-abc-789" in commit_body
assert "Agent: codex-cli" in commit_body
assert "Wip-Purpose: verify session trailer checkpointing" in commit_body
PY

echo "git-fleet smoke tests passed"
