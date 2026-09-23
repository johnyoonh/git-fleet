#!/usr/bin/env python3
"""Automatically reconcile repository worktrees according to policy levels."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_module(path: Path, name: str) -> Any:
    if not path.is_file():
        raise RuntimeError(f"missing {name}: {path}")
    spec = importlib.util.spec_from_file_location(name.replace(" ", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_engine() -> Any:
    path = Path(
        os.environ.get(
            "GIT_SYNC_ENGINE",
            Path(__file__).resolve().parent / "repo_sync.py",
        )
    ).expanduser()
    return load_module(path, "git_sync_shared_engine")


def load_policy_and_runner() -> tuple[Any, Any]:
    module_root = Path(
        os.environ.get(
            "GIT_SYNC_MODULE_ROOT",
            Path(__file__).resolve().parent,
        )
    ).expanduser()
    if not module_root.is_dir():
        raise RuntimeError(f"missing git-sync module root: {module_root}")
    sys.path.insert(0, str(module_root))
    policy_path = Path(
        os.environ.get(
            "GIT_SYNC_POLICY_MODULE",
            module_root / "automation_policy.py",
        )
    ).expanduser()
    runner_path = Path(
        os.environ.get(
            "GIT_SYNC_RUNNER_MODULE",
            module_root / "reconcile_runner.py",
        )
    ).expanduser()
    policy = load_module(policy_path, "git_sync_automation_policy")
    runner = load_module(runner_path, "git_sync_reconcile_runner")
    return policy, runner


def parser(levels: tuple[str, ...]) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--registry",
        default=os.environ.get("GIT_FLEET_REGISTRY", os.environ.get("GIT_SYNC_REGISTRY", "~/.config/git-fleet/repos.tsv")),
        help="repository policy registry",
    )
    result.add_argument(
        "--dynamic-registry",
        default=os.environ.get(
            "GIT_FLEET_DYNAMIC_REGISTRY",
            os.environ.get("GIT_SYNC_DYNAMIC_REGISTRY", "~/.local/state/git-fleet/discovered-repos.tsv"),
        ),
        help="runtime repository registry",
    )
    result.add_argument(
        "--policy",
        default=os.environ.get("GIT_FLEET_POLICY", os.environ.get("GIT_SYNC_POLICY", "~/.config/git-fleet/policy.toml")),
        help="tracked automation policy",
    )
    result.add_argument(
        "--policy-state",
        default=None,
        help="runtime automation policy overrides",
    )
    result.add_argument(
        "--level",
        choices=levels,
        help="temporary global automation level",
    )
    result.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="temporary feature override; may be repeated",
    )
    result.add_argument(
        "--root",
        action="append",
        default=[],
        help="repository discovery root",
    )
    result.add_argument(
        "--registry-only",
        action="store_true",
        help="disable unregistered discovery",
    )
    result.add_argument(
        "--slug",
        action="append",
        default=[],
        help="limit to one repository slug",
    )
    result.add_argument(
        "--only",
        dest="slug",
        action="append",
        help="alias for --slug",
    )
    result.add_argument(
        "--repo",
        action="append",
        default=[],
        help="limit to one or more repository paths",
    )
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--json", action="store_true")
    result.add_argument("--hook-timeout-seconds", type=float, default=300.0)
    result.add_argument("--resolver-timeout-seconds", type=float, default=120.0)
    return result


def source_rank(source: str) -> int:
    return {"static": 0, "dynamic": 1, "discovered": 2}.get(source, 9)


def main(argv: list[str] | None = None) -> int:
    try:
        engine = load_engine()
        policy_module, runner = load_policy_and_runner()
    except RuntimeError as error:
        print(f"git-fleet sync: {error}", file=sys.stderr)
        return 127

    args = parser(policy_module.LEVELS).parse_args(argv)
    try:
        cli_overrides = dict(
            policy_module.parse_assignment(value) for value in args.set
        )
        base_policy_path = Path(args.policy).expanduser()
        state_policy_path = (
            Path(args.policy_state).expanduser()
            if args.policy_state
            else policy_module.default_state_path()
        )
        policy_document = policy_module.load_document(
            base_policy_path,
            state_policy_path,
        )
    except policy_module.PolicyError as error:
        print(f"git-fleet sync: {error}", file=sys.stderr)
        return 2

    registry_path = Path(args.registry).expanduser()
    dynamic_registry_path = Path(args.dynamic_registry).expanduser()
    static_registry = engine.load_registry(registry_path)
    dynamic_registry = (
        engine.load_registry(dynamic_registry_path)
        if dynamic_registry_path.is_file()
        else {}
    )

    entries: dict[Path, tuple[Any, str]] = {
        path: (policy, "dynamic")
        for path, policy in dynamic_registry.items()
    }
    entries.update(
        {path: (policy, "static") for path, policy in static_registry.items()}
    )

    global_policy = policy_module.effective_policy(
        policy_document,
        slug="example/repository",
        sync_mode="auto",
        lease="required",
        cli_level=args.level,
        cli_overrides=cli_overrides,
    )
    repository_settings = dict(policy_document.get("repositories", {}))
    repository_discovery_override = any(
        bool(dict(settings).get("discover_unregistered", False))
        or dict(settings).get("level") in {"full", "reconcile"}
        for settings in repository_settings.values()
    )
    should_discover = (
        not args.registry_only
        and (
            global_policy.discover_unregistered
            or repository_discovery_override
        )
    )
    if should_discover:
        roots = [
            Path(value).expanduser().resolve()
            for value in (
                args.root
                or getattr(engine, "DEFAULT_ROOTS", ("~/repos",))
            )
        ]
        for repo in engine.discover_repositories(roots):
            if repo in entries:
                continue
            remote_url = engine.output(
                repo,
                "config",
                "--get",
                "remote.origin.url",
            )
            slug = engine.normalize_remote(remote_url) if remote_url else ""
            if slug:
                entries[repo] = (
                    engine.RepoPolicy(slug, repo, "auto", "required"),
                    "discovered",
                )

    requested_repos = {Path(p).expanduser().resolve() for p in args.repo}
    for raw_repo in args.repo:
        repo_path = Path(raw_repo).expanduser().resolve()
        if repo_path not in entries and repo_path.is_dir():
            remote_url = engine.output(
                repo_path,
                "config",
                "--get",
                "remote.origin.url",
            )
            slug = engine.normalize_remote(remote_url) if remote_url else ""
            if slug:
                entries[repo_path] = (
                    engine.RepoPolicy(slug, repo_path, "auto", "required"),
                    "cli",
                )

    requested = set(args.slug)
    grouped: dict[str, list[Any]] = {}
    results: list[Any] = []
    for raw_path, (repo_policy, source) in entries.items():
        canonical_path = Path(raw_path).expanduser().resolve()
        if requested_repos and canonical_path not in requested_repos:
            continue
        if requested and repo_policy.slug not in requested:
            continue
        try:
            automation = policy_module.effective_policy(
                policy_document,
                slug=str(repo_policy.slug),
                sync_mode=str(repo_policy.sync_mode),
                lease=str(repo_policy.lease),
                cli_level=args.level,
                cli_overrides=cli_overrides,
            )
        except policy_module.PolicyError as error:
            results.append(
                runner.SyncResult(
                    ts=engine.timestamp(),
                    event="ERROR",
                    repo=str(raw_path),
                    slug=str(repo_policy.slug),
                    detail=str(error),
                )
            )
            continue
        if not automation.mutate:
            continue
        if source == "discovered" and not automation.discover_unregistered:
            continue
        grouped.setdefault(str(repo_policy.slug), []).append(
            runner.Candidate(repo_policy, automation, source)
        )

    candidates: list[Any] = []
    for slug, items in sorted(grouped.items()):
        duplicate_policy = policy_module.effective_policy(
            policy_document,
            slug=slug,
            sync_mode="auto",
            lease="required",
            cli_level=args.level,
            cli_overrides=cli_overrides,
        )
        unique: dict[str, Any] = {}
        for item in items:
            path = str(Path(item.policy.path).expanduser().resolve())
            existing = unique.get(path)
            if (
                existing is None
                or source_rank(item.source) < source_rank(existing.source)
            ):
                unique[path] = item
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                source_rank(item.source),
                len(str(Path(item.policy.path).expanduser())),
                str(Path(item.policy.path).expanduser()),
            ),
        )
        if len(ordered) > 1 and duplicate_policy.duplicate_strategy == "stop":
            results.append(
                runner.SyncResult(
                    ts=engine.timestamp(),
                    event="ERROR",
                    repo=", ".join(
                        str(Path(item.policy.path).expanduser())
                        for item in ordered
                    ),
                    slug=slug,
                    detail="multiple mutation targets and duplicate_strategy=stop",
                )
            )
            continue
        if duplicate_policy.duplicate_strategy == "first":
            ordered = ordered[:1]
        candidates.extend(ordered)

    candidates.sort(
        key=lambda item: (
            str(item.policy.slug),
            str(Path(item.policy.path).expanduser()),
        )
    )
    results.extend(
        runner.sync_one(
            engine,
            candidate,
            dry_run=args.dry_run,
            hook_timeout=args.hook_timeout_seconds,
            resolver_timeout=args.resolver_timeout_seconds,
        )
        for candidate in candidates
    )

    if args.json:
        for item in results:
            print(json.dumps(item.json_record(), sort_keys=True))
    elif not results:
        print("git-fleet sync: no automatic mutation targets matched")
    else:
        for item in results:
            hook = (
                f" hook={item.hook_status}"
                if item.hook_status != "none"
                else ""
            )
            print(
                f"{item.event:<10} {item.slug:<36} {item.detail}{hook}".rstrip()
            )
    failures = {"ERROR", "HOOK_ERROR"}
    return 1 if any(item.event in failures for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
