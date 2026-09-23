#!/usr/bin/env python3
"""Automatically reconcile repository worktrees according to policy levels."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_LEGACY = _ROOT / "sync_legacy.py"
_CHECKPOINT = _ROOT / "sync_checkpoint.py"
_PUBLISH = _ROOT / "sync_publish.py"
_ENTRY_NAME = __name__

globals()["__name__"] = "git_fleet_legacy_embedded"
exec(compile(_LEGACY.read_text(encoding="utf-8"), str(_LEGACY), "exec"), globals())
globals()["__name__"] = _ENTRY_NAME
exec(compile(_CHECKPOINT.read_text(encoding="utf-8"), str(_CHECKPOINT), "exec"), globals())
exec(compile(_PUBLISH.read_text(encoding="utf-8"), str(_PUBLISH), "exec"), globals())

if _ENTRY_NAME == "__main__":
    raise SystemExit(main())
