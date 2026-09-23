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
PY

echo "git-fleet smoke tests passed"
