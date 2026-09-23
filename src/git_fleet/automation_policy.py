#!/usr/bin/env python3
"""Policy profiles and overrides for automatic repository reconciliation."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

POLICY_VERSION = 1
LEVELS = ("full", "reconcile", "safe", "fetch")
BOOL_KEYS = {
    "mutate",
    "discover_unregistered",
    "switch_default",
    "snapshot_dirty",
    "rebase_local",
    "recover_detached",
    "respect_leases",
    "submodules",
}
ENUM_KEYS = {
    "conflict_strategy": {"llm", "local", "stop"},
    "duplicate_strategy": {"all", "first", "stop"},
}
POLICY_KEYS = BOOL_KEYS | set(ENUM_KEYS)
NON_MUTATING_MODES = {
    "fetch",
    "fetch-only",
    "off",
    "yadm-fetch",
    "live",
    "agent",
    "deploy",
    "vendor",
}
MODE_LEVELS = {
    "full": "full",
    "full-submodules": "full",
    "reconcile": "reconcile",
    "reconcile-submodules": "reconcile",
    "safe": "safe",
    "safe-submodules": "safe",
    "ff-only": "safe",
    "ff-only-submodules": "safe",
}
SUBMODULE_MODES = {
    "auto-submodules",
    "canonical-submodules",
    "full-submodules",
    "reconcile-submodules",
    "safe-submodules",
    "ff-only-submodules",
}


class PolicyError(ValueError):
    """Raised when a policy file or override is invalid."""


@dataclass(frozen=True)
class AutomationPolicy:
    level: str
    mutate: bool
    discover_unregistered: bool
    switch_default: bool
    snapshot_dirty: bool
    rebase_local: bool
    recover_detached: bool
    conflict_strategy: str
    duplicate_strategy: str
    respect_leases: bool
    submodules: bool = False

    def json_record(self) -> dict[str, object]:
        return asdict(self)


PROFILES: dict[str, AutomationPolicy] = {
    "full": AutomationPolicy(
        level="full",
        mutate=True,
        discover_unregistered=True,
        switch_default=True,
        snapshot_dirty=True,
        rebase_local=True,
        recover_detached=True,
        conflict_strategy="llm",
        duplicate_strategy="all",
        respect_leases=True,
    ),
    "reconcile": AutomationPolicy(
        level="reconcile",
        mutate=True,
        discover_unregistered=True,
        switch_default=True,
        snapshot_dirty=True,
        rebase_local=True,
        recover_detached=True,
        conflict_strategy="local",
        duplicate_strategy="all",
        respect_leases=True,
    ),
    "safe": AutomationPolicy(
        level="safe",
        mutate=True,
        discover_unregistered=False,
        switch_default=True,
        snapshot_dirty=False,
        rebase_local=False,
        recover_detached=False,
        conflict_strategy="stop",
        duplicate_strategy="first",
        respect_leases=True,
    ),
    "fetch": AutomationPolicy(
        level="fetch",
        mutate=False,
        discover_unregistered=False,
        switch_default=False,
        snapshot_dirty=False,
        rebase_local=False,
        recover_detached=False,
        conflict_strategy="stop",
        duplicate_strategy="first",
        respect_leases=True,
    ),
}


def default_base_path() -> Path:
    return Path(
        os.environ.get(
            "GIT_FLEET_POLICY",
            os.environ.get("GIT_SYNC_POLICY", Path.home() / ".config/git-fleet/policy.toml"),
        )
    ).expanduser()


def default_state_path() -> Path:
    explicit = os.environ.get("GIT_FLEET_POLICY_STATE") or os.environ.get("GIT_SYNC_POLICY_STATE")
    if explicit:
        return Path(explicit).expanduser()
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    ).expanduser()
    return state_home / "git-fleet/policy.toml"


def parse_scalar(key: str, value: object) -> object:
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        raise PolicyError(f"{key} must be true or false")
    if key in ENUM_KEYS:
        normalized = str(value).strip().lower()
        if normalized not in ENUM_KEYS[key]:
            allowed = ", ".join(sorted(ENUM_KEYS[key]))
            raise PolicyError(f"{key} must be one of: {allowed}")
        return normalized
    raise PolicyError(f"unknown policy setting: {key}")


def parse_assignment(value: str) -> tuple[str, object]:
    if "=" not in value:
        raise PolicyError(f"override must use key=value: {value!r}")
    key, raw_value = value.split("=", 1)
    key = key.strip().replace("-", "_")
    if not key:
        raise PolicyError("override key cannot be empty")
    return key, parse_scalar(key, raw_value)


def read_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise PolicyError(f"invalid TOML in {path}: {error}") from error
    if not isinstance(payload, dict):
        raise PolicyError(f"policy root must be a table: {path}")
    validate_document(payload, source=path)
    return payload


def validate_document(payload: Mapping[str, Any], *, source: Path | str) -> None:
    version = payload.get("version", POLICY_VERSION)
    if version != POLICY_VERSION:
        raise PolicyError(
            f"{source}: policy version must be {POLICY_VERSION}, found {version!r}"
        )
    level = payload.get("level", "safe")
    if level not in LEVELS:
        raise PolicyError(f"{source}: unknown automation level: {level!r}")
    defaults = payload.get("defaults", {})
    if not isinstance(defaults, Mapping):
        raise PolicyError(f"{source}: defaults must be a table")
    validate_settings(defaults, source=f"{source}:defaults")
    repositories = payload.get("repositories", {})
    if not isinstance(repositories, Mapping):
        raise PolicyError(f"{source}: repositories must be a table")
    for slug, settings in repositories.items():
        if not isinstance(slug, str) or "/" not in slug:
            raise PolicyError(f"{source}: invalid repository key: {slug!r}")
        if not isinstance(settings, Mapping):
            raise PolicyError(f"{source}: repository {slug!r} must be a table")
        repo_level = settings.get("level")
        if repo_level is not None and repo_level not in LEVELS:
            raise PolicyError(
                f"{source}: repository {slug!r} has unknown level {repo_level!r}"
            )
        validate_settings(
            {key: value for key, value in settings.items() if key != "level"},
            source=f"{source}:repositories.{slug}",
        )


def validate_settings(settings: Mapping[str, Any], *, source: str) -> None:
    for key, value in settings.items():
        normalized = str(key).replace("-", "_")
        if normalized not in POLICY_KEYS:
            raise PolicyError(f"{source}: unknown policy setting: {key}")
        parse_scalar(normalized, value)


def merge_documents(base: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    level = state.get("level", base.get("level", "safe"))
    defaults = dict(base.get("defaults", {}))
    defaults.update(state.get("defaults", {}))
    repositories: dict[str, dict[str, Any]] = {
        str(slug): dict(settings)
        for slug, settings in dict(base.get("repositories", {})).items()
    }
    for slug, settings in dict(state.get("repositories", {})).items():
        repositories.setdefault(str(slug), {}).update(dict(settings))
    merged: dict[str, Any] = {
        "version": POLICY_VERSION,
        "level": level,
        "defaults": defaults,
        "repositories": repositories,
    }
    validate_document(merged, source="merged policy")
    return merged


def load_document(base_path: Path, state_path: Path) -> dict[str, Any]:
    return merge_documents(read_document(base_path), read_document(state_path))


def apply_settings(
    policy: AutomationPolicy,
    settings: Mapping[str, Any],
) -> AutomationPolicy:
    updates: dict[str, object] = {}
    for raw_key, value in settings.items():
        if raw_key == "level":
            continue
        key = str(raw_key).replace("-", "_")
        updates[key] = parse_scalar(key, value)
    return replace(policy, **updates)


def effective_policy(
    document: Mapping[str, Any],
    *,
    slug: str,
    sync_mode: str = "auto",
    lease: str = "required",
    cli_level: str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AutomationPolicy:
    repository_settings = dict(
        dict(document.get("repositories", {})).get(slug, {})
    )
    level = cli_level or str(document.get("level", "safe"))
    if level not in PROFILES:
        raise PolicyError(f"unknown automation level: {level!r}")

    policy = PROFILES[level]
    policy = apply_settings(policy, dict(document.get("defaults", {})))

    # A repository-level preset is a complete tone-down/tone-up override, not
    # merely another feature layered onto global defaults. Path-specific mode
    # presets are stronger still. Repository feature settings then fine-tune
    # the selected preset, and one-run CLI settings remain highest precedence.
    repo_level = repository_settings.get("level")
    if repo_level is not None:
        level = str(repo_level)
        policy = PROFILES[level]
    mode = sync_mode.strip().lower()
    if mode in MODE_LEVELS:
        level = MODE_LEVELS[mode]
        policy = PROFILES[level]
    policy = apply_settings(policy, repository_settings)
    if cli_overrides:
        policy = apply_settings(policy, cli_overrides)

    if mode in NON_MUTATING_MODES:
        policy = replace(policy, mutate=False)
    if mode in SUBMODULE_MODES:
        policy = replace(policy, submodules=True)
    if lease == "ignore":
        policy = replace(policy, respect_leases=False)
    return replace(policy, level=level)


def runtime_document(path: Path) -> dict[str, Any]:
    payload = read_document(path)
    if not payload:
        return {
            "version": POLICY_VERSION,
            "defaults": {},
            "repositories": {},
        }
    payload.setdefault("version", POLICY_VERSION)
    payload.setdefault("defaults", {})
    payload.setdefault("repositories", {})
    return payload


def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_runtime_document(payload: Mapping[str, Any]) -> str:
    lines = [f"version = {POLICY_VERSION}"]
    if "level" in payload:
        lines.extend(("", f"level = {toml_quote(str(payload['level']))}"))
    defaults = dict(payload.get("defaults", {}))
    if defaults:
        lines.extend(("", "[defaults]"))
        for key in sorted(defaults):
            lines.append(f"{key} = {render_value(defaults[key])}")
    for slug in sorted(dict(payload.get("repositories", {}))):
        settings = dict(dict(payload["repositories"])[slug])
        if not settings:
            continue
        lines.extend(("", f"[repositories.{toml_quote(slug)}]"))
        if "level" in settings:
            lines.append(f"level = {toml_quote(str(settings.pop('level')))}")
        for key in sorted(settings):
            lines.append(f"{key} = {render_value(settings[key])}")
    return "\n".join(lines).rstrip() + "\n"


def render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return toml_quote(str(value))


def write_runtime_document(path: Path, payload: Mapping[str, Any]) -> None:
    validate_document(
        {
            "version": payload.get("version", POLICY_VERSION),
            "level": payload.get("level", "full"),
            "defaults": payload.get("defaults", {}),
            "repositories": payload.get("repositories", {}),
        },
        source=path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(render_runtime_document(payload), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
