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
