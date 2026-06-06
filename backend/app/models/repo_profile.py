"""
RepoProfile — the structured summary of a cloned repository.

This is the *only* thing the LLM (generator) sees. It is NOT the raw repo.
Every field is derived by deterministic analysis of manifest files and file
patterns, which means:
  - Token cost stays bounded regardless of repo size.
  - No signal-to-noise problem: node_modules, lockfiles, test fixtures, and
    generated files are excluded before this object is built.
  - The LLM just reasons about structured facts, not an arbitrary file tree.
"""

from __future__ import annotations

from pydantic import BaseModel

# Key files are truncated to this many characters before being sent to the LLM.
# ~4 000 chars ≈ 1 000 tokens — enough for a requirements.txt or package.json,
# but won't bloat the prompt if someone checks in a minified bundle by mistake.
MAX_KEY_FILE_CHARS: int = 4_000


class KeyFile(BaseModel):
    """A manifest or entry file whose content is forwarded to the generator."""

    path: str    # relative path from repo root, e.g. "requirements.txt"
    content: str  # raw text, hard-capped at MAX_KEY_FILE_CHARS


class RepoProfile(BaseModel):
    """
    Compact, structured description of a GitHub repository.

    Built by the analyzer (Phase 2); consumed by the generator (Phase 3).
    """

    url: str = ""

    # Primary language / ecosystem.
    # Values: "python" | "javascript" | "typescript" | "go" | "rust" | "java"
    #         | "ruby" | "php" | "html" | "unknown"
    language: str = "unknown"

    # Web framework or runtime flavour ("flask", "express", "next", "gin", …).
    framework: str | None = None

    # Relative path to the application entry file ("app.py", "src/index.ts", …).
    entrypoint: str | None = None

    # Shell command that starts the app ("uvicorn app:app", "node index.js", …).
    start_command: str | None = None

    # Declared language / runtime version ("3.11", "18", "1.21", "1.75.0", …).
    runtime_version: str | None = None

    # Port the container should expose (derived from framework defaults,
    # env-var usage, or code patterns).
    exposed_port: int | None = None

    # A few manifests + the entrypoint file, truncated, for the LLM prompt.
    key_files: list[KeyFile] = []

    # Convenience flags so the generator can avoid redundant work.
    has_dockerfile: bool = False
    has_docker_compose: bool = False

    # Package manager / build tool ("pip", "npm", "yarn", "go", "cargo", …).
    build_tool: str | None = None
