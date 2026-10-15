"""Local publication policy independent of the credential's GitHub bypass rights."""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote, urlparse


def github_slug(remote: str) -> str | None:
    """Recognize github.com remotes without misclassifying local test repositories."""
    if remote.startswith("git@github.com:"):
        path = remote.split(":", 1)[1]
    else:
        parsed = urlparse(remote)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise ValueError("invalid GitHub remote")
    return path


def publication_block_reason(remote: str, branch: str, *, request=None) -> str:
    """Never push a GitHub default/protected branch, including with an admin token.

    Unknown API/permission state fails closed. Non-GitHub transports retain the
    existing publish opt-in; this function does not infer enterprise host policy.
    """
    try:
        slug = github_slug(remote)
        if slug is None:
            return ""
        if request is None:
            def request(path):
                result = subprocess.run(["gh", "api", path], capture_output=True,
                                        text=True, timeout=30, check=True)
                return json.loads(result.stdout)
        info = request(f"repos/{slug}")
        default = info.get("default_branch")
        if not isinstance(default, str) or not default:
            raise ValueError("default branch unknown")
        if branch == default:
            return "default branch requires a PR; publish never grants bypass authority"
        encoded = quote(branch, safe="")
        details = request(f"repos/{slug}/branches/{encoded}")
        rules = request(f"repos/{slug}/rules/branches/{encoded}")
        if type(details.get("protected")) is not bool or not isinstance(rules, list):
            raise ValueError("protection state unknown")
        if details["protected"] or rules:
            return "protected branch requires a PR; credential bypass rights are ignored"
        return ""
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError):
        return "GitHub protection could not be verified; publication deferred (fail closed)"
