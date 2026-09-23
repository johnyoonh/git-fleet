#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
mkdir -p "$PREFIX/bin"
ln -sfn "$ROOT/bin/git-fleet" "$PREFIX/bin/git-fleet"
printf 'installed %s\n' "$PREFIX/bin/git-fleet"
