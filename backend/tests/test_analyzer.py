"""
Unit tests for the repository analyzer.

Each test runs `analyze_repo` against a small fixture directory that lives at
``tests/fixtures/<name>/`` — no git clone or network call is needed.

Fixture inventory:
  flask_app/    — Python + Flask (requirements.txt + app.py with __main__ guard)
  express_app/  — Node.js + Express (package.json with start script + index.js)
  static_site/  — Plain HTML (index.html only, no build system)
  no_entrypoint/ — Python utility files, no manifest, no __main__ guard
"""

from pathlib import Path

import pytest

from app.agent.analyzer import analyze_repo

FIXTURES = Path(__file__).parent / "fixtures"


# ── Flask app ─────────────────────────────────────────────────────────────────


def test_flask_language():
    profile = analyze_repo(FIXTURES / "flask_app")
    assert profile.language == "python"


def test_flask_framework():
    profile = analyze_repo(FIXTURES / "flask_app")
    assert profile.framework == "flask"


def test_flask_entrypoint():
    profile = analyze_repo(FIXTURES / "flask_app")
    # "app.py" is the first candidate in _ENTRY_CANDIDATES["python"]
    assert profile.entrypoint == "app.py"


def test_flask_port():
    profile = analyze_repo(FIXTURES / "flask_app")
    # Flask default port comes from _FRAMEWORK_PORTS
    assert profile.exposed_port == 5000


def test_flask_build_tool():
    profile = analyze_repo(FIXTURES / "flask_app")
    assert profile.build_tool == "pip"


def test_flask_key_files_include_requirements():
    profile = analyze_repo(FIXTURES / "flask_app")
    paths = [kf.path for kf in profile.key_files]
    assert "requirements.txt" in paths


def test_flask_no_existing_dockerfile():
    profile = analyze_repo(FIXTURES / "flask_app")
    assert profile.has_dockerfile is False


# ── Express app ───────────────────────────────────────────────────────────────


def test_express_language():
    profile = analyze_repo(FIXTURES / "express_app")
    assert profile.language == "javascript"


def test_express_framework():
    profile = analyze_repo(FIXTURES / "express_app")
    assert profile.framework == "express"


def test_express_start_command():
    profile = analyze_repo(FIXTURES / "express_app")
    # Comes from scripts.start in package.json
    assert profile.start_command == "node index.js"


def test_express_entrypoint():
    profile = analyze_repo(FIXTURES / "express_app")
    # Comes from "main": "index.js" in package.json (file exists)
    assert profile.entrypoint == "index.js"


def test_express_port():
    profile = analyze_repo(FIXTURES / "express_app")
    # Express default port comes from _FRAMEWORK_PORTS
    assert profile.exposed_port == 3000


def test_express_runtime_version():
    profile = analyze_repo(FIXTURES / "express_app")
    # "engines.node": ">=18.0.0" → major = "18"
    assert profile.runtime_version == "18"


def test_express_key_files_include_package_json():
    profile = analyze_repo(FIXTURES / "express_app")
    paths = [kf.path for kf in profile.key_files]
    assert "package.json" in paths


# ── Static site ───────────────────────────────────────────────────────────────


def test_static_language():
    profile = analyze_repo(FIXTURES / "static_site")
    assert profile.language == "html"


def test_static_entrypoint():
    profile = analyze_repo(FIXTURES / "static_site")
    assert profile.entrypoint == "index.html"


def test_static_no_framework():
    profile = analyze_repo(FIXTURES / "static_site")
    assert profile.framework is None


def test_static_no_port():
    profile = analyze_repo(FIXTURES / "static_site")
    # A static site has no listen port
    assert profile.exposed_port is None


def test_static_no_start_command():
    profile = analyze_repo(FIXTURES / "static_site")
    assert profile.start_command is None


# ── No entrypoint ─────────────────────────────────────────────────────────────


def test_no_entrypoint_language():
    profile = analyze_repo(FIXTURES / "no_entrypoint")
    # No manifest → extension scan finds .py files → python
    assert profile.language == "python"


def test_no_entrypoint_is_none():
    profile = analyze_repo(FIXTURES / "no_entrypoint")
    # utils.py / models.py have no __main__ guard and aren't in the candidates list
    assert profile.entrypoint is None


def test_no_entrypoint_no_framework():
    profile = analyze_repo(FIXTURES / "no_entrypoint")
    assert profile.framework is None


def test_no_entrypoint_does_not_raise():
    # The analyzer must never crash — it returns a best-effort profile.
    profile = analyze_repo(FIXTURES / "no_entrypoint")
    assert profile is not None


# ── Cross-fixture sanity ──────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture_name", [
    "flask_app", "express_app", "static_site", "no_entrypoint",
])
def test_profile_is_valid_pydantic(fixture_name):
    """RepoProfile is always a valid model — all fields have correct types."""
    profile = analyze_repo(FIXTURES / fixture_name)
    # Pydantic model_dump() validates field types; if the model is corrupt,
    # this raises ValidationError.
    data = profile.model_dump()
    assert isinstance(data["language"], str)
    assert isinstance(data["key_files"], list)
    assert isinstance(data["has_dockerfile"], bool)
