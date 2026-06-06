"""
Repository analyzer — build a structured RepoProfile from a cloned repo.

Calling ``analyze_repo(path)`` returns a :class:`RepoProfile` that captures
everything the generator (Gemini) needs to know about the project without ever
seeing the raw file tree.  Detection is entirely deterministic:

  1. Parse manifest files (requirements.txt, package.json, go.mod, …) to get
     language, framework, runtime version, start command, and build tool.
  2. Fall back to counting file extensions if no manifest is found.
  3. Locate the application entrypoint from a priority list, then from a scan
     for language-specific main-guard patterns (``if __name__ == "__main__":``).
  4. Resolve the listen port from framework defaults, then from a code scan for
     common PORT-assignment patterns.
  5. Collect the content of key manifest + entry files (truncated) for the
     LLM prompt.

Why not dump the whole repo to the LLM?
  - Token cost: even a small project can be hundreds of thousands of tokens.
  - Signal-to-noise: 99 % of file content is irrelevant (tests, assets,
    lockfiles, generated code).  Giving the model less, better information
    produces better Dockerfiles than overwhelming it with everything.
  - Reliability: structured extraction is deterministic; scraping free-form
    prose for a Dockerfile is fragile and can hallucinate paths.
  This is the same reasoning behind RAG over document search.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from app.models.repo_profile import KeyFile, MAX_KEY_FILE_CHARS, RepoProfile

# ── Constants ──────────────────────────────────────────────────────────────────

# Directories that are never walked — generated, vendor, or tooling artefacts.
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".venv", "venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "out", ".output",
    "target", ".gradle", ".mvn",
    "vendor", ".cargo",
    ".next", ".nuxt",
    "coverage", ".nyc_output", "htmlcov",
})

# Framework name → default listen port (used when port is not declared in code).
_FRAMEWORK_PORTS: dict[str, int] = {
    "flask": 5000,
    "django": 8000,
    "fastapi": 8000,
    "starlette": 8000,
    "tornado": 8888,
    "aiohttp": 8080,
    "sanic": 8000,
    "litestar": 8000,
    "express": 3000,
    "fastify": 3000,
    "koa": 3000,
    "hapi": 3000,
    "nest": 3000,
    "next": 3000,
    "nuxt": 3000,
    "rails": 3000,
    "sinatra": 4567,
    "gin": 8080,
    "echo": 8080,
    "fiber": 3000,
    "spring": 8080,
    "actix": 8080,
    "axum": 3000,
    "rocket": 8000,
}

# Entrypoint candidates in priority order, by language.
_ENTRY_CANDIDATES: dict[str, list[str]] = {
    "python": [
        "app.py", "main.py", "server.py", "run.py",
        "wsgi.py", "asgi.py", "manage.py", "application.py",
    ],
    "javascript": [
        "index.js", "server.js", "app.js",
        "src/index.js", "src/server.js", "src/app.js",
    ],
    "typescript": [
        "index.ts", "server.ts", "app.ts",
        "src/index.ts", "src/server.ts", "src/app.ts",
    ],
    "go": ["main.go", "cmd/main.go"],
    "ruby": ["app.rb", "config.ru", "server.rb", "main.rb"],
    "rust": ["src/main.rs"],
}

# Manifest filenames to include in key_files (content sent to the LLM).
_MANIFEST_NAMES: tuple[str, ...] = (
    "requirements.txt", "pyproject.toml", "setup.py",
    "package.json", "go.mod", "Cargo.toml", "Gemfile",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
)

# Pre-compiled pattern for port detection in source files.
_PORT_RE = re.compile(r"(?:PORT|port)[=:\s(\"']+(\d{3,5})")


# ── Public API ────────────────────────────────────────────────────────────────


def analyze_repo(repo_root: Path, url: str = "") -> RepoProfile:
    """
    Analyse a cloned repository at *repo_root* and return a :class:`RepoProfile`.

    Safe to call on any directory; never raises — returns a best-effort profile
    even for repos with unusual or absent manifests.
    """
    profile = RepoProfile(url=url)

    _detect_from_manifests(repo_root, profile)

    if profile.language == "unknown":
        _detect_from_extensions(repo_root, profile)

    if profile.entrypoint is None:
        _find_entrypoint(repo_root, profile)

    # Port: framework default first, then code scan.
    if profile.exposed_port is None and profile.framework:
        profile.exposed_port = _FRAMEWORK_PORTS.get(profile.framework)
    if profile.exposed_port is None:
        _scan_for_port(repo_root, profile)

    _collect_key_files(repo_root, profile)

    profile.has_dockerfile = (repo_root / "Dockerfile").exists()
    profile.has_docker_compose = (
        (repo_root / "docker-compose.yml").exists()
        or (repo_root / "docker-compose.yaml").exists()
    )

    return profile


# ── Manifest detection ────────────────────────────────────────────────────────


def _detect_from_manifests(repo_root: Path, profile: RepoProfile) -> None:
    """Update *profile* in-place from the first recognised manifest at repo root."""

    # Python — requirements.txt wins over pyproject.toml when both exist
    if (repo_root / "requirements.txt").exists():
        profile.language = "python"
        profile.build_tool = "pip"
        _apply_requirements_txt(repo_root / "requirements.txt", profile)
        return
    if (repo_root / "pyproject.toml").exists():
        profile.language = "python"
        profile.build_tool = "pip"
        _apply_pyproject_toml(repo_root / "pyproject.toml", profile)
        return
    if (repo_root / "setup.py").exists() or (repo_root / "setup.cfg").exists():
        profile.language = "python"
        profile.build_tool = "pip"
        return

    # Node.js
    if (repo_root / "package.json").exists():
        profile.language = "javascript"
        profile.build_tool = "npm"
        _apply_package_json(repo_root, repo_root / "package.json", profile)
        return

    # Go
    if (repo_root / "go.mod").exists():
        profile.language = "go"
        profile.build_tool = "go"
        _apply_go_mod(repo_root / "go.mod", profile)
        return

    # Rust
    if (repo_root / "Cargo.toml").exists():
        profile.language = "rust"
        profile.build_tool = "cargo"
        return

    # Java (Maven)
    if (repo_root / "pom.xml").exists():
        profile.language = "java"
        profile.build_tool = "maven"
        return

    # Java (Gradle)
    if (repo_root / "build.gradle").exists() or (repo_root / "build.gradle.kts").exists():
        profile.language = "java"
        profile.build_tool = "gradle"
        return

    # Ruby
    if (repo_root / "Gemfile").exists():
        profile.language = "ruby"
        profile.build_tool = "bundler"
        _apply_gemfile(repo_root / "Gemfile", profile)
        return

    # PHP
    if (repo_root / "composer.json").exists():
        profile.language = "php"
        profile.build_tool = "composer"
        return

    # HTML static site (fallback — only if no other manifest was found)
    if (repo_root / "index.html").exists():
        profile.language = "html"
        profile.entrypoint = "index.html"


# ── Individual manifest parsers ───────────────────────────────────────────────


def _apply_requirements_txt(path: Path, profile: RepoProfile) -> None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return
    deps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", ".")):
            continue
        # "Flask>=3.0.0 ; python_requires..." → "flask"
        pkg = re.split(r"[>=<!\[;@\s]", line)[0].lower()
        if pkg:
            deps.append(pkg)
    fw = _match_python_framework(deps)
    if fw:
        profile.framework = fw


def _apply_pyproject_toml(path: Path, profile: RepoProfile) -> None:
    try:
        data = tomllib.loads(path.read_text(errors="ignore"))
    except Exception:
        return

    # PEP 621 [project] table
    project = data.get("project", {})
    if "requires-python" in project:
        # ">=3.11" → "3.11"
        profile.runtime_version = re.sub(r"[^0-9.]", "", str(project["requires-python"])).lstrip(".")

    deps: list[str] = []
    for dep in project.get("dependencies", []):
        pkg = re.split(r"[>=<!\[;@\s]", str(dep))[0].lower()
        if pkg:
            deps.append(pkg)

    # Poetry [tool.poetry.dependencies]
    poetry = data.get("tool", {}).get("poetry", {})
    poetry_deps = poetry.get("dependencies", {})
    deps.extend(k.lower() for k in poetry_deps if k.lower() != "python")
    if "python" in poetry_deps and not profile.runtime_version:
        profile.runtime_version = re.sub(r"[^0-9.]", "", str(poetry_deps["python"])).lstrip(".")

    fw = _match_python_framework(deps)
    if fw:
        profile.framework = fw


def _apply_package_json(repo_root: Path, path: Path, profile: RepoProfile) -> None:
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return

    # Runtime version from engines.node
    node_ver = (data.get("engines") or {}).get("node", "")
    if node_ver:
        stripped = re.sub(r"[^0-9.]", "", str(node_ver)).lstrip(".")
        major = stripped.split(".")[0] if stripped else ""
        if major:
            profile.runtime_version = major

    # Collect all deps (prod + dev) for framework detection
    all_deps: dict[str, str] = {}
    all_deps.update(data.get("dependencies") or {})
    all_deps.update(data.get("devDependencies") or {})

    # TypeScript?
    if "typescript" in all_deps or (repo_root / "tsconfig.json").exists():
        profile.language = "typescript"

    # Start command from scripts
    scripts = data.get("scripts") or {}
    start = scripts.get("start") or scripts.get("serve")
    if start:
        profile.start_command = start

    # Entrypoint from "main" field (only if the file actually exists)
    main_field = data.get("main")
    if main_field and profile.entrypoint is None:
        candidate = repo_root / main_field
        if candidate.exists():
            profile.entrypoint = str(Path(main_field))

    fw = _match_node_framework(all_deps)
    if fw:
        profile.framework = fw


def _apply_go_mod(path: Path, profile: RepoProfile) -> None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return
    m = re.search(r"^go\s+(\d+\.\d+)", text, re.MULTILINE)
    if m:
        profile.runtime_version = m.group(1)


def _apply_gemfile(path: Path, profile: RepoProfile) -> None:
    try:
        text = path.read_text(errors="ignore").lower()
    except OSError:
        return
    if "rails" in text:
        profile.framework = "rails"
    elif "sinatra" in text:
        profile.framework = "sinatra"


# ── Framework matchers ────────────────────────────────────────────────────────


def _match_python_framework(deps: list[str]) -> str | None:
    # Checked in priority order (most specific first to avoid false positives
    # e.g. "starlette" is a dep of fastapi).
    priority = [
        "django", "fastapi", "flask", "starlette",
        "tornado", "aiohttp", "sanic", "litestar",
    ]
    for fw in priority:
        if any(d.startswith(fw) for d in deps):
            return fw
    return None


def _match_node_framework(deps: dict[str, str]) -> str | None:
    checks = [
        ("next", "next"),
        ("nuxt", "nuxt"),
        ("@nestjs/core", "nest"),
        ("express", "express"),
        ("fastify", "fastify"),
        ("koa", "koa"),
        ("@hapi/hapi", "hapi"),
    ]
    for dep_name, fw in checks:
        if dep_name in deps:
            return fw
    return None


# ── Extension fallback ────────────────────────────────────────────────────────


def _detect_from_extensions(repo_root: Path, profile: RepoProfile) -> None:
    """Guess language by counting source files — used only when no manifest exists."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java",
        ".php": "php", ".cs": "csharp", ".cpp": "cpp", ".c": "c",
    }
    counts: dict[str, int] = {}
    for fpath in repo_root.rglob("*"):
        if not fpath.is_file():
            continue
        if any(part in _SKIP_DIRS for part in fpath.parts):
            continue
        lang = ext_map.get(fpath.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1

    if counts:
        profile.language = max(counts, key=lambda k: counts[k])
    elif (repo_root / "index.html").exists():
        profile.language = "html"
        profile.entrypoint = "index.html"


# ── Entrypoint detection ──────────────────────────────────────────────────────


def _find_entrypoint(repo_root: Path, profile: RepoProfile) -> None:
    # 1. Check priority candidate list
    for rel in _ENTRY_CANDIDATES.get(profile.language, []):
        if (repo_root / rel).exists():
            profile.entrypoint = rel
            return

    # 2. Python: scan for if __name__ == "__main__" guard
    if profile.language == "python":
        found = _scan_for_python_main(repo_root)
        if found:
            profile.entrypoint = found


def _scan_for_python_main(repo_root: Path) -> str | None:
    """Find the first .py file that contains an ``if __name__ == '__main__':`` guard."""
    for fpath in sorted(repo_root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in fpath.parts):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if '__name__ == "__main__"' in text or "__name__ == '__main__'" in text:
            return str(fpath.relative_to(repo_root))
    return None


# ── Port detection ────────────────────────────────────────────────────────────


def _scan_for_port(repo_root: Path, profile: RepoProfile) -> None:
    """Scan source files for PORT variable assignments."""
    ext = {
        "python": ".py", "javascript": ".js", "typescript": ".ts",
        "go": ".go", "ruby": ".rb",
    }.get(profile.language)
    if not ext:
        return

    for fpath in repo_root.rglob(f"*{ext}"):
        if any(part in _SKIP_DIRS for part in fpath.parts):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _PORT_RE.finditer(text):
            port = int(m.group(1))
            if 1024 <= port <= 65535:
                profile.exposed_port = port
                return  # first match wins


# ── Key file collection ───────────────────────────────────────────────────────


def _collect_key_files(repo_root: Path, profile: RepoProfile) -> None:
    """
    Populate ``profile.key_files`` with manifests + entrypoint content.

    Each file is read once and truncated to ``MAX_KEY_FILE_CHARS``.
    """
    seen: set[str] = set()

    def _add(rel: str) -> None:
        if rel in seen:
            return
        fpath = repo_root / rel
        if not fpath.is_file():
            return
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if len(content) > MAX_KEY_FILE_CHARS:
            content = content[:MAX_KEY_FILE_CHARS] + "\n... [truncated]"
        profile.key_files.append(KeyFile(path=rel, content=content))
        seen.add(rel)

    # Manifests at root
    for name in _MANIFEST_NAMES:
        _add(name)

    # Application entrypoint
    if profile.entrypoint:
        _add(profile.entrypoint)

    # First README found
    for name in ("README.md", "README.rst", "README.txt", "README"):
        if (repo_root / name).exists():
            _add(name)
            break
