#!/usr/bin/env python3
"""Repository fleet discovery, status, and fetch management."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_ROOTS = (
    "~/src",
    "~/projects",
    "~/github",
    "~/repos",
)
GITHUB_METADATA_TTL_SECONDS = 15 * 60
GITHUB_METADATA_RETRY_SECONDS = 5 * 60
GITHUB_METADATA_BATCH_SIZE = 40
GITHUB_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RepoPolicy:
    slug: str
    path: Path
    sync_mode: str = "auto"
    lease: str = "required"


@dataclass(frozen=True)
class RepoResult:
    ts: str
    event: str
    repo: Path
    slug: str = ""
    branch: str = ""
    upstream: str = ""
    dirty: bool | None = None
    ahead: int | None = None
    behind: int | None = None
    detail: str = ""
    leases: int = 0
    remote_only: bool = False
    commit_sha: str = ""
    commit_at: str = ""
    commit_subject: str = ""
    visibility: str = ""
    archived: bool | None = None
    github_pushed_at: str = ""
    github_updated_at: str = ""
    default_branch: str = ""
    default_commit_sha: str = ""
    default_commit_at: str = ""
    default_commit_subject: str = ""

    def state(self) -> str:
        if self.remote_only:
            return "REMOTE"
        if self.event in {"ERROR", "LEASED", "LOCKED", "SKIP"}:
            return self.event
        if not self.upstream:
            return "NO UPSTREAM"
        if (self.ahead or 0) > 0 and (self.behind or 0) > 0:
            return "DIVERGED"
        if (self.behind or 0) > 0:
            return "BEHIND"
        if (self.ahead or 0) > 0:
            return "AHEAD"
        if self.dirty:
            return "DIRTY"
        return "ALIGNED"

    def json_record(self) -> dict[str, object]:
        record = asdict(self)
        record["repo"] = str(self.repo)
        record["state"] = self.state()
        return record


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def output(repo: Path, *args: str) -> str:
    proc = run(repo, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def run_yadm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["yadm", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def yadm_output(*args: str) -> str:
    proc = run_yadm(*args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def normalize_remote(url: str) -> str:
    value = url.strip()
    prefixes = (
        "git@github.com:",
        "git@github.com/",
        "https://github.com/",
        "http://github.com/",
        "ssh://git@github.com/",
        "ssh://git@github.com:",
        "git+ssh://",
    )
    for prefix in prefixes:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return value.removesuffix(".git")


def repo_key(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:24]


def state_root() -> Path:
    explicit = os.environ.get("GIT_FLEET_STATE_DIR") or os.environ.get("REPO_SYNC_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "repo-sync"
    return Path.home() / ".local" / "state" / "git-fleet"


def aisess_state_root() -> Path:
    explicit = os.environ.get("GIT_FLEET_LEASE_STATE_DIR") or os.environ.get("AISESS_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "aisess"
    return Path.home() / ".local" / "state" / "aisess"


def github_metadata_cache_path() -> Path:
    explicit = os.environ.get("REPO_SYNC_GITHUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    return state_root() / "github-metadata.json"


def load_github_metadata(path: Path | None = None) -> dict[str, object]:
    target = path or github_metadata_cache_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "repositories": {}}
    if not isinstance(data, dict) or not isinstance(data.get("repositories"), dict):
        return {"version": 1, "repositories": {}}
    return data


def save_github_metadata(data: dict[str, object], path: Path | None = None) -> None:
    target = path or github_metadata_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def parse_iso_timestamp(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def age_text(value: object, *, now: float | None = None) -> str:
    epoch = parse_iso_timestamp(value)
    if epoch is None:
        return "—"
    seconds = max(0, int((time.time() if now is None else now) - epoch))
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d" if days < 365 else f"{days // 365}y"


def github_cache_status(data: dict[str, object], *, now: float | None = None) -> tuple[bool, str]:
    current = time.time() if now is None else now
    generated = parse_iso_timestamp(data.get("generated_at"))
    attempted = parse_iso_timestamp(data.get("last_attempt_at"))
    ttl = max(0, int(os.environ.get("REPO_SYNC_GITHUB_TTL_SECONDS", GITHUB_METADATA_TTL_SECONDS)))
    retry = max(0, int(os.environ.get("REPO_SYNC_GITHUB_RETRY_SECONDS", GITHUB_METADATA_RETRY_SECONDS)))
    fresh = generated is not None and current - generated < ttl
    cooling_down = attempted is not None and current - attempted < retry
    if generated is None:
        label = "empty"
    else:
        age = age_text(data.get("generated_at"), now=current)
        label = "cached now" if age == "now" else f"cached {age} ago"
    return (not fresh and not cooling_down), label


def github_graphql_query(slugs: list[str]) -> str:
    fields: list[str] = []
    for index, slug in enumerate(slugs):
        owner, name = slug.split("/", 1)
        fields.append(
            f'r{index}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{'
            "nameWithOwner visibility isArchived pushedAt updatedAt "
            "defaultBranchRef{name target{... on Commit{oid committedDate messageHeadline}}}}"
        )
    return "query{" + " ".join(fields) + "}"


def fetch_github_metadata_batch(slugs: list[str]) -> dict[str, dict[str, object]]:
    query = github_graphql_query(slugs)
    process = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
        env={**os.environ, "GH_FORCE_TTY": "0", "NO_COLOR": "1"},
    )
    if process.returncode != 0:
        detail = process.stderr.strip().splitlines()[-1] if process.stderr.strip() else "gh api failed"
        raise RuntimeError(detail)
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid GitHub response: {exc}") from exc
    raw_data = payload.get("data", {}) if isinstance(payload, dict) else {}
    records: dict[str, dict[str, object]] = {}
    for index, slug in enumerate(slugs):
        raw = raw_data.get(f"r{index}") if isinstance(raw_data, dict) else None
        if not isinstance(raw, dict):
            continue
        branch = raw.get("defaultBranchRef")
        target = branch.get("target") if isinstance(branch, dict) else None
        records[slug] = {
            "visibility": str(raw.get("visibility") or "").lower(),
            "archived": bool(raw.get("isArchived")),
            "pushed_at": str(raw.get("pushedAt") or ""),
            "updated_at": str(raw.get("updatedAt") or ""),
            "default_branch": str(branch.get("name") or "") if isinstance(branch, dict) else "",
            "default_commit_sha": str(target.get("oid") or "") if isinstance(target, dict) else "",
            "default_commit_at": str(target.get("committedDate") or "") if isinstance(target, dict) else "",
            "default_commit_subject": str(target.get("messageHeadline") or "") if isinstance(target, dict) else "",
        }
    return records


def refresh_github_metadata(slugs: list[str], path: Path | None = None) -> int:
    target = path or github_metadata_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        cache = load_github_metadata(target)
        cache["last_attempt_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        repositories = dict(cache.get("repositories", {}))
        try:
            if shutil.which("gh") is None:
                raise RuntimeError("gh unavailable")
            for offset in range(0, len(slugs), GITHUB_METADATA_BATCH_SIZE):
                repositories.update(fetch_github_metadata_batch(slugs[offset : offset + GITHUB_METADATA_BATCH_SIZE]))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            cache["repositories"] = repositories
            cache["last_error"] = str(exc)
            save_github_metadata(cache, target)
            return 1
        cache.update(
            version=1,
            generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            repositories=repositories,
            last_error="",
        )
        save_github_metadata(cache, target)
    return 0


def spawn_github_metadata_refresh(slugs: list[str], path: Path | None = None) -> bool:
    valid = sorted({slug for slug in slugs if GITHUB_SLUG.fullmatch(slug)})
    if not valid:
        return False
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--refresh-github-cache",
        "--metadata-cache",
        str(path or github_metadata_cache_path()),
    ]
    for slug in valid:
        command.extend(("--metadata-slug", slug))
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_aisess_leases(repo: Path) -> list[dict[str, object]]:
    directory = aisess_state_root() / "repo-leases" / repo_key(repo)
    if not directory.exists():
        return []
    leases: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
        except Exception:
            path.unlink(missing_ok=True)
            continue
        if not pid_alive(pid):
            path.unlink(missing_ok=True)
            continue
        data["lease_file"] = str(path)
        leases.append(data)
    try:
        directory.rmdir()
    except OSError:
        pass
    return leases


@contextmanager
def repo_lock(repo: Path) -> Iterator[bool]:
    directory = state_root() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{repo_key(repo)}.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nrepo={repo}\nstarted={timestamp()}\n")
        handle.flush()
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def load_allowlist(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def load_watchlist(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    slugs: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        slug = raw.split("\t", 1)[0].strip()
        if "/" in slug:
            slugs.add(slug)
    return slugs


def load_registry(path: Path) -> dict[Path, RepoPolicy]:
    policies: dict[Path, RepoPolicy] = {}
    if not path.is_file():
        return policies
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) < 4:
            print(f"git-fleet: invalid registry row {number}: {raw!r}", file=sys.stderr)
            continue
        slug, raw_path, sync_mode, lease = fields[:4]
        repo = Path(os.path.expandvars(raw_path)).expanduser().resolve()
        policies[repo] = RepoPolicy(slug, repo, sync_mode, lease)
    return policies


def discover_repositories(roots: list[Path]) -> list[Path]:
    repos: set[Path] = set()
    ignored = {".cache", ".venv", "node_modules", "Library", ".Trash"}
    for root in roots:
        if not root.is_dir():
            continue
        if (root / ".git").exists():
            repos.add(root.resolve())
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in ignored]
            current_path = Path(current)
            if ".git" in dirs:
                repos.add(current_path.resolve())
                dirs.remove(".git")
            elif ".git" in files:
                repos.add(current_path.resolve())
                dirs[:] = []
    return sorted(repos)


def parse_status_snapshot(text: str) -> tuple[str, str, bool, int | None, int | None]:
    branch = "HEAD"
    upstream = ""
    ahead: int | None = None
    behind: int | None = None
    dirty = False
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            value = line.removeprefix("# branch.head ")
            branch = "HEAD" if value == "(detached)" else value
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            match = re.fullmatch(r"# branch\.ab \+(\d+) -(\d+)", line)
            if match:
                ahead, behind = int(match.group(1)), int(match.group(2))
        elif line and not line.startswith("# "):
            dirty = True
    return branch, upstream, dirty, ahead, behind


def repo_status(repo: Path) -> tuple[str, str, bool, int | None, int | None]:
    process = run(repo, "status", "--porcelain=v2", "--branch")
    if process.returncode != 0:
        return "HEAD", "", False, None, None
    return parse_status_snapshot(process.stdout)


def yadm_status() -> tuple[str, str, bool, int | None, int | None]:
    process = run_yadm("status", "--porcelain=v2", "--branch")
    if process.returncode != 0:
        return "HEAD", "", False, None, None
    return parse_status_snapshot(process.stdout)


def local_commit_info(repo: Path) -> tuple[str, str, str]:
    command = ["git", "-C", str(repo), "log", "-1", "--format=%h%x00%cI%x00%s"]
    if repo.resolve() == Path.home().resolve() and shutil.which("yadm"):
        command = ["yadm", "log", "-1", "--format=%h%x00%cI%x00%s"]
    process = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    parts = process.stdout.rstrip("\n").split("\0", 2) if process.returncode == 0 else []
    return tuple(parts) if len(parts) == 3 else ("", "", "")


def enrich_results(
    results: list[RepoResult], metadata: dict[str, object]
) -> list[RepoResult]:
    repositories = metadata.get("repositories", {})
    cached = repositories if isinstance(repositories, dict) else {}
    enriched: list[RepoResult] = []
    for item in results:
        commit_sha = commit_at = commit_subject = ""
        if not item.remote_only and item.repo.is_dir():
            commit_sha, commit_at, commit_subject = local_commit_info(item.repo)
        raw = cached.get(item.slug, {}) if item.slug else {}
        github = raw if isinstance(raw, dict) else {}
        enriched.append(
            replace(
                item,
                commit_sha=commit_sha,
                commit_at=commit_at,
                commit_subject=commit_subject,
                visibility=str(github.get("visibility") or ""),
                archived=github.get("archived") if isinstance(github.get("archived"), bool) else None,
                github_pushed_at=str(github.get("pushed_at") or ""),
                github_updated_at=str(github.get("updated_at") or ""),
                default_branch=str(github.get("default_branch") or ""),
                default_commit_sha=str(github.get("default_commit_sha") or ""),
                default_commit_at=str(github.get("default_commit_at") or ""),
                default_commit_subject=str(github.get("default_commit_subject") or ""),
            )
        )
    return enriched


def inspect_yadm(
    repo: Path,
    *,
    policy: RepoPolicy,
    allowlist: set[str] | None,
    do_fetch: bool,
) -> RepoResult | None:
    now = timestamp()
    if shutil.which("yadm") is None:
        return RepoResult(now, "ERROR", repo, policy.slug, detail="yadm unavailable")
    remote_url = yadm_output("remote", "get-url", "origin")
    slug = normalize_remote(remote_url)
    if not remote_url or slug != policy.slug:
        return RepoResult(now, "ERROR", repo, policy.slug, detail="yadm origin mismatch")
    if allowlist is not None and slug not in allowlist:
        return None
    leases = live_aisess_leases(repo)
    if leases and policy.lease != "ignore":
        branch, upstream, dirty, ahead, behind = yadm_status()
        return RepoResult(
            now,
            "LEASED",
            repo,
            slug,
            branch,
            upstream,
            dirty,
            ahead,
            behind,
            ", ".join(f"pid={item.get('pid')}" for item in leases),
            len(leases),
        )
    if not do_fetch:
        branch, upstream, dirty, ahead, behind = yadm_status()
        return RepoResult(
            now,
            "STATUS",
            repo,
            slug,
            branch,
            upstream,
            dirty,
            ahead,
            behind,
            status_detail(
                upstream=upstream,
                dirty=dirty,
                ahead=ahead,
                behind=behind,
                fetched=False,
            ),
        )
    with repo_lock(repo) as acquired:
        if not acquired:
            branch, upstream, dirty, ahead, behind = yadm_status()
            return RepoResult(
                now,
                "LOCKED",
                repo,
                slug,
                branch,
                upstream,
                dirty,
                ahead,
                behind,
                "another sync process owns the yadm lock",
            )
        fetched = run_yadm("fetch", "--quiet", "--prune")
        if fetched.returncode != 0:
            detail = (
                fetched.stderr.strip().splitlines()[-1]
                if fetched.stderr.strip()
                else "yadm fetch failed"
            )
            return RepoResult(now, "ERROR", repo, slug, detail=detail)
        branch, upstream, dirty, ahead, behind = yadm_status()
        return RepoResult(
            now,
            "FETCHED",
            repo,
            slug,
            branch,
            upstream,
            dirty,
            ahead,
            behind,
            status_detail(
                upstream=upstream,
                dirty=dirty,
                ahead=ahead,
                behind=behind,
                fetched=True,
            ),
        )


def status_detail(
    *, upstream: str, dirty: bool, ahead: int | None, behind: int | None, fetched: bool
) -> str:
    parts = ["fetch-only" if fetched else "read-only", f"dirty={'yes' if dirty else 'no'}"]
    if upstream:
        parts.extend((f"ahead={ahead or 0}", f"behind={behind or 0}"))
    else:
        parts.append("upstream=none")
    return " ".join(parts)


def inspect_repo(
    repo: Path,
    *,
    policy: RepoPolicy | None,
    allowlist: set[str] | None,
    do_fetch: bool,
) -> RepoResult | None:
    if policy and policy.sync_mode == "yadm-fetch":
        return inspect_yadm(
            repo,
            policy=policy,
            allowlist=allowlist,
            do_fetch=do_fetch,
        )
    now = timestamp()
    remote_url = output(repo, "config", "--get", "remote.origin.url")
    if not remote_url:
        return RepoResult(now, "SKIP", repo, detail="no remote.origin.url")
    slug = normalize_remote(remote_url)
    if allowlist is not None and slug not in allowlist:
        return None
    if policy and policy.sync_mode == "off":
        return RepoResult(now, "SKIP", repo, slug=slug, detail="registry sync_mode=off")

    leases = live_aisess_leases(repo)
    if leases and (policy is None or policy.lease != "ignore"):
        branch, upstream, dirty, ahead, behind = repo_status(repo)
        detail = ", ".join(
            f"pid={item.get('pid')} preset={item.get('preset')}" for item in leases
        )
        return RepoResult(
            now,
            "LEASED",
            repo,
            slug,
            branch,
            upstream,
            dirty,
            ahead,
            behind,
            detail,
            len(leases),
        )

    if not do_fetch:
        branch, upstream, dirty, ahead, behind = repo_status(repo)
        return RepoResult(
            now,
            "STATUS",
            repo,
            slug,
            branch,
            upstream,
            dirty,
            ahead,
            behind,
            status_detail(
                upstream=upstream,
                dirty=dirty,
                ahead=ahead,
                behind=behind,
                fetched=False,
            ),
        )

    with repo_lock(repo) as acquired:
        if not acquired:
            branch, upstream, dirty, ahead, behind = repo_status(repo)
            return RepoResult(
                now,
                "LOCKED",
                repo,
                slug,
                branch,
                upstream,
                dirty,
                ahead,
                behind,
                "another sync process owns the repository lock",
            )
        fetch = run(repo, "fetch", "--quiet", "--prune")
        if fetch.returncode != 0:
            branch = output(repo, "branch", "--show-current") or "HEAD"
            detail = fetch.stderr.strip().splitlines()[-1] if fetch.stderr.strip() else "fetch failed"
            return RepoResult(now, "ERROR", repo, slug, branch, detail=detail)
        branch, upstream, dirty, ahead, behind = repo_status(repo)
        return RepoResult(
            now,
            "FETCHED",
            repo,
            slug,
            branch,
            upstream,
            dirty,
            ahead,
            behind,
            status_detail(
                upstream=upstream,
                dirty=dirty,
                ahead=ahead,
                behind=behind,
                fetched=True,
            ),
        )


class Theme:
    def __init__(self, enabled: bool, ascii_only: bool) -> None:
        self.enabled = enabled
        self.ascii_only = ascii_only
        self.reset = "\033[0m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.colors = {
            "ALIGNED": "\033[32m",
            "DIRTY": "\033[33m",
            "AHEAD": "\033[36m",
            "BEHIND": "\033[35m",
            "DIVERGED": "\033[31m",
            "ERROR": "\033[31m",
            "LEASED": "\033[34m",
            "LOCKED": "\033[34m",
            "NO UPSTREAM": "\033[33m",
            "SKIP": "\033[2m",
            "REMOTE": "\033[2m",
        }

    def paint(self, text: str, style: str) -> str:
        if not self.enabled:
            return text
        code = self.colors.get(style, "")
        return f"{code}{text}{self.reset}" if code else text

    @property
    def symbols(self) -> dict[str, str]:
        if self.ascii_only:
            return {
                "ALIGNED": "OK",
                "DIRTY": "*",
                "AHEAD": "^",
                "BEHIND": "v",
                "DIVERGED": "!",
                "ERROR": "X",
                "LEASED": "L",
                "LOCKED": "#",
                "NO UPSTREAM": "?",
                "SKIP": "-",
                "REMOTE": "R",
            }
        return {
            "ALIGNED": "✓",
            "DIRTY": "●",
            "AHEAD": "↑",
            "BEHIND": "↓",
            "DIVERGED": "↕",
            "ERROR": "✕",
            "LEASED": "◆",
            "LOCKED": "◇",
            "NO UPSTREAM": "?",
            "SKIP": "–",
            "REMOTE": "R",
        }


def terminal_color_enabled(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return (
        sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )


def display_name(result: RepoResult) -> str:
    if result.slug and not result.slug.startswith(("/", ".", "~")) and "://" not in result.slug:
        return result.slug
    try:
        return f"~/{result.repo.relative_to(Path.home())}"
    except ValueError:
        return str(result.repo)


def clip(text: str, width: int) -> str:
    if width <= 1:
        return text[:width]
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def bar(value: int, maximum: int, width: int, ascii_only: bool) -> str:
    if width <= 0:
        return ""
    fill = 0 if maximum <= 0 else round(width * value / maximum)
    full, empty = ("#", ".") if ascii_only else ("█", "░")
    return full * fill + empty * (width - fill)


def push_text(item: RepoResult) -> str:
    if item.remote_only:
        return "remote"
    if not item.upstream:
        return "no upstream"
    ahead, behind = item.ahead or 0, item.behind or 0
    if ahead and behind:
        return f"+{ahead}/-{behind}"
    if ahead:
        return f"+{ahead} pending"
    return f"behind {behind}" if behind else "pushed"


def visibility_text(item: RepoResult) -> str:
    value = (item.visibility or "unknown").upper()
    return value + ("/A" if item.archived else "")


def render_plain(results: list[RepoResult]) -> None:
    for item in results:
        event = item.event if item.event != "STATUS" else item.state()
        print(f"{item.ts} {event:<11}: {item.repo} [{item.branch}] {item.detail}".rstrip())


def render_json(results: list[RepoResult]) -> None:
    for item in results:
        print(json.dumps(item.json_record(), sort_keys=True))


def render_table(results: list[RepoResult], theme: Theme) -> None:
    width = max(88, min(shutil.get_terminal_size((120, 24)).columns, 180))
    show_activity = width >= 120
    show_commit = width >= 156
    fixed = 14 + 10 + 14 + 14 + 9 + 6
    if show_activity:
        fixed += 22
    if show_commit:
        fixed += 24
    repo_width = max(20, width - fixed)
    columns = [
        ("STATE", 14),
        ("ACCESS", 10),
        ("REPOSITORY", repo_width),
        ("BRANCH", 14),
        ("PUSH", 14),
        ("GH PUSH", 9),
    ]
    if show_activity:
        columns.extend((("UPDATED", 9), ("ARCH", 5)))
    if show_commit:
        columns.append(("LATEST COMMIT", 23))
    header = " ".join(f"{label:<{size}}" for label, size in columns)
    print(theme.bold + header + theme.reset)
    rule = "─" if not theme.ascii_only else "-"
    print(rule * min(width, len(header)))
    symbols = theme.symbols
    ordered = sorted(
        results,
        key=lambda row: (row.state() == "ALIGNED", row.state(), display_name(row)),
    )
    for item in ordered:
        state = item.state()
        label = f"{symbols[state]} {state}"
        branch = (item.branch or "—") + ("*" if item.dirty else "")
        values = [
            f"{clip(label, 14):<14}",
            f"{clip(visibility_text(item), 10):<10}",
            f"{clip(display_name(item), repo_width):<{repo_width}}",
            f"{clip(branch, 14):<14}",
            f"{clip(push_text(item), 14):<14}",
            f"{age_text(item.default_commit_at or item.github_pushed_at):<9}",
        ]
        if show_activity:
            values.extend(
                (
                    f"{age_text(item.github_updated_at):<9}",
                    f"{'yes' if item.archived else ('no' if item.archived is False else '—'):<5}",
                )
            )
        if show_commit:
            commit = " ".join(
                part
                for part in (
                    age_text(item.commit_at),
                    item.commit_sha,
                    item.commit_subject,
                )
                if part and part != "—"
            ) or "—"
            values.append(f"{clip(commit, 23):<23}")
        row = " ".join(values).rstrip()
        print(theme.paint(row, state))


def render_dashboard(
    results: list[RepoResult],
    theme: Theme,
    operation: str,
    metadata_status: str = "",
) -> None:
    width = max(88, min(shutil.get_terminal_size((120, 24)).columns, 180))
    inner = width - 2
    if theme.ascii_only:
        tl, horizontal, vertical, tr, bl, br = "+", "-", "|", "+", "+", "+"
    else:
        tl, horizontal, vertical, tr, bl, br = "╭", "─", "│", "╮", "╰", "╯"
    title = f" Git repository fleet · {operation.upper()} · {timestamp()} "
    print(tl + horizontal * (width - 2) + tr)
    print(vertical + theme.bold + title.center(inner) + theme.reset + vertical)
    print(bl + horizontal * (width - 2) + br)

    total = len(results)
    counts: dict[str, int] = {}
    for item in results:
        counts[item.state()] = counts.get(item.state(), 0) + 1
    aligned = counts.get("ALIGNED", 0)
    dirty = sum(1 for item in results if item.dirty)
    ahead_repos = sum(1 for item in results if (item.ahead or 0) > 0)
    behind_repos = sum(1 for item in results if (item.behind or 0) > 0)
    errors = counts.get("ERROR", 0)
    leases = counts.get("LEASED", 0)
    ratio = 0 if total == 0 else round(aligned * 100 / total)
    alignment = bar(aligned, total, min(36, max(16, width // 4)), theme.ascii_only)
    print(
        f"\n{theme.bold}Alignment{theme.reset}  {aligned}/{total} "
        f"[{theme.paint(alignment, 'ALIGNED')}] {ratio}%"
    )
    print(
        f"Total {total}   Aligned {aligned}   Dirty {dirty}   Ahead {ahead_repos}   "
        f"Behind {behind_repos}   Leased {leases}   Errors {errors}"
    )
    if metadata_status:
        print(f"GitHub metadata: {metadata_status}")

    print(f"\n{theme.bold}State distribution{theme.reset}")
    order = [
        "ALIGNED",
        "DIRTY",
        "AHEAD",
        "BEHIND",
        "DIVERGED",
        "LEASED",
        "LOCKED",
        "NO UPSTREAM",
        "ERROR",
        "REMOTE",
        "SKIP",
    ]
    max_count = max(counts.values(), default=1)
    chart_width = min(32, max(12, width // 5))
    for state in order:
        count = counts.get(state, 0)
        if count == 0:
            continue
        chart = bar(count, max_count, chart_width, theme.ascii_only)
        print(f"  {state:<12} {count:>3} {theme.paint(chart, state)}")

    divergent = [item for item in results if (item.ahead or 0) or (item.behind or 0)]
    if divergent:
        print(f"\n{theme.bold}Commit divergence{theme.reset}")
        max_divergence = max(max(item.ahead or 0, item.behind or 0) for item in divergent)
        div_width = min(20, max(8, width // 7))
        name_width = max(24, min(44, width - (div_width * 2) - 20))
        ordered_divergence = sorted(
            divergent,
            key=lambda row: (row.ahead or 0) + (row.behind or 0),
            reverse=True,
        )
        for item in ordered_divergence:
            ahead = item.ahead or 0
            behind = item.behind or 0
            up = bar(ahead, max_divergence, div_width, theme.ascii_only)
            down = bar(behind, max_divergence, div_width, theme.ascii_only)
            print(
                f"  {clip(display_name(item), name_width):<{name_width}} "
                f"↑ {ahead:>3} {theme.paint(up, 'AHEAD')}  "
                f"↓ {behind:>3} {theme.paint(down, 'BEHIND')}"
            )

    print(f"\n{theme.bold}Repository detail{theme.reset}")
    render_table(results, theme)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Inspect or fetch repositories without modifying local history or worktrees."
    )
    result.add_argument("command", nargs="?", default="fetch", choices=("fetch", "status"))
    result.add_argument("--root", action="append", default=[])
    result.add_argument(
        "--allowlist",
        default=os.environ.get("GIT_SYNC_CHATGPT_ALLOWLIST"),
    )
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
        help="runtime repository policy registry",
    )
    result.add_argument(
        "--watchlist",
        default=os.environ.get(
            "GIT_FLEET_PR_WATCHLIST",
            os.environ.get("GIT_SYNC_PR_WATCHLIST", "~/.local/state/git-fleet/pr-watches.tsv"),
        ),
        help="runtime remote repository watchlist",
    )
    result.add_argument(
        "--slug",
        action="append",
        default=[],
        help="limit inspection to one or more repository slugs",
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
        help="limit inspection to one or more repository paths",
    )
    result.add_argument("--registry-only", action="store_true")
    result.add_argument("--no-registry", action="store_true")
    result.add_argument("--local-only", action="store_true", help="exclude remote-only watches")
    result.add_argument(
        "--format",
        choices=("auto", "dashboard", "table", "plain", "json"),
        default="auto",
    )
    result.add_argument("--json", action="store_true", help="compatibility alias for --format json")
    result.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    result.add_argument("--ascii", action="store_true", help="use ASCII-only dashboard symbols")
    result.add_argument("--discover", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--discover-via-gh", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--owner", help=argparse.SUPPRESS)
    result.add_argument("--max-repos", help=argparse.SUPPRESS)
    result.add_argument("--source-only", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--no-source-only", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--no-auto-fix", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--no-llm-resolve", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--no-migrate-main", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--no-merge-prs", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--refresh-github-cache", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--metadata-cache", help=argparse.SUPPRESS)
    result.add_argument("--metadata-slug", action="append", default=[], help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.refresh_github_cache:
        target = Path(args.metadata_cache).expanduser() if args.metadata_cache else None
        return refresh_github_metadata(args.metadata_slug, target)
    registry_path = Path(args.registry).expanduser()
    dynamic_registry_path = Path(args.dynamic_registry).expanduser()
    watchlist_path = Path(args.watchlist).expanduser()
    if args.no_registry:
        registry: dict[Path, RepoPolicy] = {}
        dynamic_registry: dict[Path, RepoPolicy] = {}
    else:
        registry = load_registry(registry_path)
        dynamic_registry = load_registry(dynamic_registry_path)
        registry = {**dynamic_registry, **registry}
    requested_slugs = set(args.slug)
    requested_repos = {Path(value).expanduser().resolve() for value in args.repo}
    roots = [Path(value).expanduser().resolve() for value in (args.root or DEFAULT_ROOTS)]
    repos = set(registry)
    if requested_repos:
        repos.update(requested_repos)
    all_registered_slugs = {p.slug for p in registry.values()}
    can_skip_discovery = bool(
        requested_repos
        or (requested_slugs and requested_slugs.issubset(all_registered_slugs))
    )
    if not args.registry_only and not can_skip_discovery:
        repos.update(discover_repositories(roots))
    allowlist = load_allowlist(Path(args.allowlist).expanduser() if args.allowlist else None)
    watched_slugs = load_watchlist(watchlist_path)
    if allowlist is not None:
        allowlist.update(policy.slug for policy in dynamic_registry.values())
        allowlist.update(watched_slugs)

    results: list[RepoResult] = []
    for repo in sorted(repos):
        if not repo.is_dir():
            continue
        if requested_repos and repo not in requested_repos:
            continue
        policy = registry.get(repo)
        if requested_slugs and policy and policy.slug not in requested_slugs:
            continue
        item = inspect_repo(
            repo,
            policy=policy,
            allowlist=allowlist,
            do_fetch=args.command == "fetch",
        )
        if item is not None:
            if requested_slugs and item.slug not in requested_slugs:
                continue
            results.append(item)

    local_slugs = {item.slug for item in results if item.slug}
    if not args.local_only and not requested_repos:
        for slug in sorted(watched_slugs - local_slugs):
            if requested_slugs and slug not in requested_slugs:
                continue
            if allowlist is not None and slug not in allowlist:
                continue
            results.append(
                RepoResult(
                    timestamp(),
                    "REMOTE",
                    Path(f"remote:{slug}"),
                    slug=slug,
                    detail="remote-only watch; local placement unavailable",
                    remote_only=True,
                )
            )

    output_format = "json" if args.json else args.format
    if output_format == "auto":
        output_format = "dashboard" if sys.stdout.isatty() else "plain"
    metadata = load_github_metadata()
    metadata_status = ""
    if output_format in {"dashboard", "table", "json"}:
        results = enrich_results(results, metadata)
    if output_format == "dashboard" and args.command == "status":
        needs_refresh, metadata_status = github_cache_status(metadata)
        if needs_refresh:
            queued = spawn_github_metadata_refresh([item.slug for item in results if item.slug])
            metadata_status += " · refresh queued" if queued else " · refresh unavailable"
    theme = Theme(terminal_color_enabled(args.color), args.ascii)
    if output_format == "json":
        render_json(results)
    elif output_format == "plain":
        render_plain(results)
    elif output_format == "table":
        render_table(results, theme)
    else:
        render_dashboard(results, theme, args.command, metadata_status)

    return 1 if any(item.event == "ERROR" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
