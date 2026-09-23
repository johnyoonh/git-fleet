#!/usr/bin/env python3
"""Inspect and change git-fleet automation levels and feature overrides."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


def module_path() -> Path:
    explicit = os.environ.get("GIT_FLEET_POLICY_MODULE") or os.environ.get("GIT_SYNC_POLICY_MODULE")
    if explicit:
        return Path(explicit).expanduser()
    return Path(__file__).resolve().parent / "automation_policy.py"


def load_policy_module() -> Any:
    path = module_path()
    if not path.is_file():
        raise RuntimeError(f"missing automation policy module: {path}")
    spec = importlib.util.spec_from_file_location("git_sync_automation_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load automation policy module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parser(levels: tuple[str, ...], keys: set[str]) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--policy", type=Path)
    result.add_argument("--state", type=Path)
    commands = result.add_subparsers(dest="command")

    show = commands.add_parser("show", help="Show the effective automation policy.")
    show.add_argument("--repo", help="Show one repository's effective policy.")
    show.add_argument("--mode", default="auto", help="Registry mode to apply when showing a repo.")
    show.add_argument("--lease", default="required")
    show.add_argument("--json", action="store_true")

    level = commands.add_parser("level", help="Set the global or repository automation level.")
    level.add_argument("value", choices=levels)
    level.add_argument("--repo")

    set_command = commands.add_parser("set", help="Set one global or repository feature override.")
    set_command.add_argument("key", choices=sorted(keys))
    set_command.add_argument("value")
    set_command.add_argument("--repo")

    unset = commands.add_parser("unset", help="Remove one global or repository feature override.")
    unset.add_argument("key", choices=sorted(keys))
    unset.add_argument("--repo")

    reset = commands.add_parser("reset", help="Clear runtime overrides globally or for one repository.")
    reset.add_argument("--repo")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        policy_module = load_policy_module()
    except RuntimeError as error:
        print(f"git-fleet policy: {error}", file=sys.stderr)
        return 127

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["show"]
    args = parser(policy_module.LEVELS, policy_module.POLICY_KEYS).parse_args(raw_argv)
    command = args.command or "show"
    if args.command is None:
        args.repo = None
        args.mode = "auto"
        args.lease = "required"
        args.json = False
    base_path = (args.policy or policy_module.default_base_path()).expanduser()
    state_path = (args.state or policy_module.default_state_path()).expanduser()

    try:
        if command == "show":
            document = policy_module.load_document(base_path, state_path)
            if args.repo:
                effective = policy_module.effective_policy(
                    document,
                    slug=args.repo,
                    sync_mode=args.mode,
                    lease=args.lease,
                )
                payload = {
                    "repository": args.repo,
                    "registry_mode": args.mode,
                    "policy": effective.json_record(),
                    "base_policy": str(base_path),
                    "runtime_policy": str(state_path),
                }
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print(f"repository: {args.repo}")
                    print(f"level: {effective.level}")
                    for key, value in effective.json_record().items():
                        if key != "level":
                            print(f"{key}: {str(value).lower() if isinstance(value, bool) else value}")
                return 0

            level = str(document.get("level", "safe"))
            effective = policy_module.effective_policy(
                document,
                slug="example/repository",
                sync_mode="auto",
                lease="required",
            )
            payload = {
                "level": level,
                "defaults": effective.json_record(),
                "repository_overrides": document.get("repositories", {}),
                "base_policy": str(base_path),
                "runtime_policy": str(state_path),
                "runtime_exists": state_path.is_file(),
            }
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"level: {level}")
                print(f"runtime: {state_path} ({'set' if state_path.is_file() else 'default'})")
                for key, value in effective.json_record().items():
                    if key != "level":
                        print(f"{key}: {str(value).lower() if isinstance(value, bool) else value}")
                repositories = dict(document.get("repositories", {}))
                if repositories:
                    print("repository overrides:")
                    for slug in sorted(repositories):
                        settings = ", ".join(
                            f"{key}={value}" for key, value in sorted(dict(repositories[slug]).items())
                        )
                        print(f"  {slug}: {settings}")
            return 0

        runtime = policy_module.runtime_document(state_path)
        if command == "level":
            if args.repo:
                runtime.setdefault("repositories", {}).setdefault(args.repo, {})["level"] = args.value
                target = args.repo
            else:
                runtime["level"] = args.value
                target = "global"
            policy_module.write_runtime_document(state_path, runtime)
            print(f"{target} automation level: {args.value}")
            return 0

        if command == "set":
            key = args.key.replace("-", "_")
            value = policy_module.parse_scalar(key, args.value)
            if args.repo:
                repositories = runtime.setdefault("repositories", {})
                repositories.setdefault(args.repo, {})[key] = value
                target = args.repo
            else:
                runtime.setdefault("defaults", {})[key] = value
                target = "global"
            policy_module.write_runtime_document(state_path, runtime)
            rendered = str(value).lower() if isinstance(value, bool) else value
            print(f"{target}: {key}={rendered}")
            return 0

        if command == "unset":
            key = args.key.replace("-", "_")
            if args.repo:
                repositories = runtime.setdefault("repositories", {})
                settings = repositories.get(args.repo, {})
                settings.pop(key, None)
                if not settings:
                    repositories.pop(args.repo, None)
                target = args.repo
            else:
                runtime.setdefault("defaults", {}).pop(key, None)
                target = "global"
            policy_module.write_runtime_document(state_path, runtime)
            print(f"{target}: cleared {key}")
            return 0

        if command == "reset":
            if args.repo:
                runtime.setdefault("repositories", {}).pop(args.repo, None)
                policy_module.write_runtime_document(state_path, runtime)
                print(f"{args.repo}: cleared all overrides")
            else:
                state_path.unlink(missing_ok=True)
                print("runtime policy reset to tracked defaults")
            return 0
    except policy_module.PolicyError as error:
        print(f"git-fleet policy: {error}", file=sys.stderr)
        return 2

    raise AssertionError(f"unhandled command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
