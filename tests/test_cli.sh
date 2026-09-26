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
assert data["defaults"]["publish"] is True
PY

python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]) / "src" / "git_fleet"))
import automation_policy

assert automation_policy.PROFILES["fetch"].publish is False
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

if ! run_publish_sync > "$TMP/publish-default.jsonl"; then
  cat "$TMP/publish-default.jsonl" >&2
  exit 1
fi
grep -q '"event": "PUBLISHED"' "$TMP/publish-default.jsonl"
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
found = set(repo_sync.discover_repositories([root]))
assert ordinary in found, found
assert linked not in found, found
assert repo_sync.discover_repositories([linked]) == []
PY

echo "git-fleet smoke tests passed"
