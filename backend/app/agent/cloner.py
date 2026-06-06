"""
Repository cloner with security guardrails.

The public API is a single context manager:

    with cloned_repo("https://github.com/owner/repo") as path:
        profile = analyze_repo(path)
    # temp dir is gone here, success or failure

Design decisions
────────────────
- URL validation runs *before* any network call (SSRF prevention): we check
  that the scheme is HTTPS and the host is exactly github.com. Any other host
  — including localhost, 127.0.0.1, or internal IPs — is rejected before git
  is invoked.
- Shallow clone (--depth 1) minimises clone time and download size.
- Hard timeout on the git subprocess: a network stall must not block the
  FastAPI worker indefinitely.
- Size cap checked after clone: keeps temp-disk usage bounded and prevents
  analysing repos that are too large to be useful (media-heavy monorepos etc.).
- `finally` block guarantees cleanup even if the caller raises inside the `with`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse

# Repo URL must be HTTPS and must be on exactly this host.
_GITHUB_HOST = "github.com"

# Path component must look like /owner/repo (optionally .git-suffixed).
# Allows alphanumerics, hyphens, underscores, and dots in each segment.
_PATH_RE = re.compile(
    r"^/(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+?)(\.git)?$"
)


# ── Custom exceptions ─────────────────────────────────────────────────────────


class InvalidRepoURLError(ValueError):
    """URL rejected by pre-clone validation — no network activity has occurred."""


class CloneError(RuntimeError):
    """git clone exited non-zero or timed out."""


class RepoTooLargeError(CloneError):
    """Cloned repo exceeds the configured size cap."""


# ── Public helpers ────────────────────────────────────────────────────────────


def validate_url(url: str) -> tuple[str, str]:
    """
    Validate *url* and return ``(owner, repo_name)``.

    Raises :exc:`InvalidRepoURLError` with a human-readable message for any
    violation.  All checks are local — no DNS lookup or network call is made.
    """
    try:
        parsed = urlparse(url.strip())
    except Exception as exc:
        raise InvalidRepoURLError(f"Could not parse URL: {exc}") from exc

    if parsed.scheme != "https":
        raise InvalidRepoURLError(
            f"Only HTTPS URLs are accepted (got scheme '{parsed.scheme}'). "
            "HTTP is rejected to prevent SSRF via redirect and mixed-content."
        )

    host = (parsed.hostname or "").lower()
    if host != _GITHUB_HOST:
        raise InvalidRepoURLError(
            f"Only github.com repos are supported (got host '{host or '(empty)'}').  "
            "Accepting arbitrary hosts would allow SSRF to internal services."
        )

    if parsed.port is not None:
        raise InvalidRepoURLError(
            "Explicit port numbers are not allowed in repo URLs."
        )

    match = _PATH_RE.match(parsed.path or "")
    if not match:
        raise InvalidRepoURLError(
            f"URL path '{parsed.path}' does not look like a GitHub repo "
            "(expected /owner/repo or /owner/repo.git)."
        )

    return match.group("owner"), match.group("repo")


@contextmanager
def cloned_repo(
    url: str,
    *,
    timeout_s: int = 60,
    max_mb: int = 200,
) -> Generator[Path, None, None]:
    """
    Context manager: shallow-clone *url* into a fresh temp dir, yield the
    path, then unconditionally delete the temp dir on exit.

    Example::

        with cloned_repo("https://github.com/pallets/flask") as repo_path:
            profile = analyze_repo(repo_path)
        # temp dir cleaned up here regardless of what happened above

    :raises InvalidRepoURLError: URL fails pre-clone validation.
    :raises CloneError: git exits non-zero or exceeds *timeout_s*.
    :raises RepoTooLargeError: repo content exceeds *max_mb* after cloning.
    """
    validate_url(url)

    tmp = Path(tempfile.mkdtemp(prefix="dockerforge_"))
    try:
        # git clone into a pre-existing empty directory works fine.
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", "--", url, str(tmp)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise CloneError(
                f"git clone timed out after {timeout_s}s — {url}"
            ) from exc

        if result.returncode != 0:
            stderr_tail = result.stderr.strip()[-600:]
            raise CloneError(
                f"git clone failed (exit {result.returncode}) for {url}\n"
                f"{stderr_tail}"
            )

        size_mb = _dir_size_mb(tmp)
        if size_mb > max_mb:
            raise RepoTooLargeError(
                f"Repo is {size_mb:.1f} MB after cloning, exceeds the "
                f"{max_mb} MB cap. Use a smaller or more focused repo."
            )

        yield tmp

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _dir_size_mb(path: Path) -> float:
    """Sum all file sizes under *path*, skipping .git metadata."""
    total = sum(
        f.stat().st_size
        for f in path.rglob("*")
        if f.is_file() and ".git" not in f.parts
    )
    return total / (1024 * 1024)
