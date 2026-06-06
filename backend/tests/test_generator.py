"""
Unit tests for the generator module.

All tests mock the Gemini SDK — no API key or network call is needed.
What we're verifying:
  1. Prompt construction contains the right information (deterministic).
  2. The function returns a valid GeneratorOutput when the mock returns parsed data.
  3. The JSON-text fallback path works when response.parsed is None.
  4. A missing API key raises GeneratorError before any API call.
  5. An SDK exception is wrapped into GeneratorError.
  6. An unparseable response is wrapped into GeneratorError.
  7. The fix_dockerfile prompt differs from the generation prompt.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agent.generator import (
    GeneratorError,
    _build_fix_prompt,
    _build_generation_prompt,
    fix_dockerfile,
    generate_dockerfile,
)
from app.config import get_settings
from app.models.generator_output import GeneratorOutput, HealthCheck
from app.models.repo_profile import KeyFile, RepoProfile


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Ensure a fresh settings parse for every test (get_settings is @lru_cache)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _flask_profile() -> RepoProfile:
    return RepoProfile(
        url="https://github.com/test/flask-app",
        language="python",
        framework="flask",
        entrypoint="app.py",
        start_command=None,
        runtime_version="3.11",
        exposed_port=5000,
        build_tool="pip",
        key_files=[
            KeyFile(path="requirements.txt", content="flask==3.0.0\ngunicorn==21.2.0"),
            KeyFile(path="app.py", content="from flask import Flask\napp = Flask(__name__)"),
        ],
    )


def _valid_output() -> GeneratorOutput:
    return GeneratorOutput(
        dockerfile=(
            "FROM python:3.11-slim AS builder\n"
            "WORKDIR /app\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n\n"
            "FROM python:3.11-slim\n"
            "RUN useradd -u 1001 appuser\n"
            "WORKDIR /app\n"
            "COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11\n"
            "COPY . .\n"
            "USER appuser\n"
            "EXPOSE 5000\n"
            "HEALTHCHECK CMD curl -f http://localhost:5000/ || exit 1\n"
            "CMD [\"gunicorn\", \"app:app\", \"--bind\", \"0.0.0.0:5000\"]\n"
        ),
        compose=None,
        run_command="docker run --rm -p 5000:5000 test-app",
        exposed_port=5000,
        healthcheck=HealthCheck(type="http", detail="/"),
        dockerignore="__pycache__/\n*.pyc\n.venv/\n",
        reasoning="Used python:3.11-slim for a small image. Multi-stage isolates build deps.",
    )


# ── Prompt construction (no SDK, no network) ──────────────────────────────────


def test_generation_prompt_contains_language():
    prompt = _build_generation_prompt(_flask_profile())
    assert "python" in prompt


def test_generation_prompt_contains_framework():
    prompt = _build_generation_prompt(_flask_profile())
    assert "flask" in prompt


def test_generation_prompt_contains_key_file_content():
    prompt = _build_generation_prompt(_flask_profile())
    assert "requirements.txt" in prompt
    assert "flask==3.0.0" in prompt


def test_generation_prompt_contains_requirements_section():
    prompt = _build_generation_prompt(_flask_profile())
    assert "multi-stage" in prompt.lower()
    assert "non-root" in prompt.lower()


def test_fix_prompt_contains_previous_dockerfile():
    prev = "FROM python:3.11\nRUN pip install flask\n"
    err = "ERROR: Could not find a version that satisfies the requirement Flask"
    prompt = _build_fix_prompt(_flask_profile(), prev, err, attempt_number=2)
    assert prev in prompt
    assert err in prompt
    assert "attempt 2" in prompt.lower()


def test_fix_prompt_differs_from_generation_prompt():
    gen_prompt = _build_generation_prompt(_flask_profile())
    fix_prompt = _build_fix_prompt(_flask_profile(), "FROM scratch\n", "err", 1)
    # Fix prompt must mention the error; generation prompt must not
    assert "Build error" in fix_prompt
    assert "Build error" not in gen_prompt


def test_no_key_files_shows_fallback():
    profile = RepoProfile(language="python", url="")
    prompt = _build_generation_prompt(profile)
    assert "no key files" in prompt.lower()


# ── Happy path — mock SDK ─────────────────────────────────────────────────────


@patch("app.agent.generator.genai.Client")
def test_generate_returns_generator_output(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    get_settings.cache_clear()

    mock_resp = MagicMock()
    mock_resp.parsed = _valid_output()
    mock_client_cls.return_value.models.generate_content.return_value = mock_resp

    result = generate_dockerfile(_flask_profile())

    assert isinstance(result, GeneratorOutput)
    assert result.exposed_port == 5000
    assert result.healthcheck.type == "http"
    assert result.healthcheck.detail == "/"
    assert "FROM python:3.11-slim" in result.dockerfile


@patch("app.agent.generator.genai.Client")
def test_generate_calls_correct_model(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")
    get_settings.cache_clear()

    mock_resp = MagicMock()
    mock_resp.parsed = _valid_output()
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    generate_dockerfile(_flask_profile())

    call_kwargs = mock_client.models.generate_content.call_args
    assert call_kwargs.kwargs["model"] == "gemini-2.0-flash"


# ── Fallback: response.parsed is None → parse response.text ──────────────────


@patch("app.agent.generator.genai.Client")
def test_fallback_json_parsing(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    get_settings.cache_clear()

    valid_json = _valid_output().model_dump_json()

    mock_resp = MagicMock()
    mock_resp.parsed = None        # trigger fallback path
    mock_resp.text = valid_json
    mock_client_cls.return_value.models.generate_content.return_value = mock_resp

    result = generate_dockerfile(_flask_profile())
    assert isinstance(result, GeneratorOutput)
    assert result.exposed_port == 5000


# ── fix_dockerfile uses the fix prompt ───────────────────────────────────────


@patch("app.agent.generator.genai.Client")
def test_fix_dockerfile_passes_error_context(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    get_settings.cache_clear()

    mock_resp = MagicMock()
    mock_resp.parsed = _valid_output()
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    fix_dockerfile(
        _flask_profile(),
        previous_dockerfile="FROM python:3.11\nRUN bad command\n",
        build_error_tail="ERROR: bad command not found",
        attempt_number=2,
    )

    call_kwargs = mock_client.models.generate_content.call_args
    prompt_sent = call_kwargs.kwargs["contents"]
    assert "bad command not found" in prompt_sent
    assert "attempt 2" in prompt_sent.lower()


# ── Error paths ───────────────────────────────────────────────────────────────


def test_missing_api_key_raises_generator_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(GeneratorError, match="GROQ_API_KEY"):
        generate_dockerfile(_flask_profile())


@patch("app.agent.generator.genai.Client")
def test_sdk_exception_wrapped_in_generator_error(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    get_settings.cache_clear()

    mock_client_cls.return_value.models.generate_content.side_effect = RuntimeError(
        "quota exceeded"
    )

    with pytest.raises(GeneratorError, match="Gemini API call failed"):
        generate_dockerfile(_flask_profile())


@patch("app.agent.generator.genai.Client")
def test_unparseable_response_raises_generator_error(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    get_settings.cache_clear()

    mock_resp = MagicMock()
    mock_resp.parsed = None
    mock_resp.text = "this is not valid json {"
    mock_client_cls.return_value.models.generate_content.return_value = mock_resp

    with pytest.raises(GeneratorError, match="Could not parse"):
        generate_dockerfile(_flask_profile())


# ── Groq / OAI-compat path ────────────────────────────────────────────────────


def _make_rate_limit_error(retry_after: float):
    """
    Build a real openai.RateLimitError that looks like a Groq/OAI-proxy 429.

    We create it properly via its constructor so isinstance checks work and
    it can actually be raised by mock side_effect machinery.
    httpx.Response requires a request to be attached; we supply a dummy one.
    """
    import json as _json

    import httpx
    from openai import RateLimitError

    body = {
        "error": {
            "message": "Provider returned error",
            "code": 429,
            "metadata": {"retry_after_seconds": retry_after},
        }
    }
    # httpx.Response.request must be set or accessing it raises RuntimeError
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(
        status_code=429,
        content=_json.dumps(body).encode(),
        headers={"Retry-After": str(int(retry_after)), "content-type": "application/json"},
        request=req,
    )
    return RateLimitError("Provider returned error", response=resp, body=body)


# ── Patch target for OAI-compat tests ─────────────────────────────────────────
#
# `from openai import OpenAI` inside _call_openai_compat resolves from the
# real `openai` module at call time — so we must patch *openai.OpenAI*, NOT
# a generator-module attribute.  Patching the source module is the canonical
# approach when the import is inside a function body.
_OAI_CLS_TARGET = "openai.OpenAI"


@patch("app.agent.generator.time.sleep")
@patch(_OAI_CLS_TARGET)
def test_openrouter_path_succeeds_on_first_try(mock_oai_cls, mock_sleep, monkeypatch):
    """When GEMINI_BASE_URL is set the OAI-compat path is used and returns output."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "")   # ensure Groq path is taken
    get_settings.cache_clear()

    valid_json = _valid_output().model_dump_json()
    mock_choice = MagicMock()
    mock_choice.message.content = valid_json
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_oai_cls.return_value.chat.completions.create.return_value = mock_response

    result = generate_dockerfile(_flask_profile())

    assert isinstance(result, GeneratorOutput)
    assert result.exposed_port == 5000
    mock_sleep.assert_not_called()  # no rate-limit wait on first success


@patch("app.agent.generator.time.sleep")
@patch(_OAI_CLS_TARGET)
def test_openrouter_retries_on_429_then_succeeds(mock_oai_cls, mock_sleep, monkeypatch):
    """A single 429 should be retried; the second call succeeds."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "")   # ensure Groq path is taken
    get_settings.cache_clear()

    rate_err = _make_rate_limit_error(retry_after=27.0)
    valid_json = _valid_output().model_dump_json()
    mock_choice = MagicMock()
    mock_choice.message.content = valid_json
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    create_mock = mock_oai_cls.return_value.chat.completions.create
    create_mock.side_effect = [rate_err, mock_response]

    result = generate_dockerfile(_flask_profile())

    assert isinstance(result, GeneratorOutput)
    assert create_mock.call_count == 2
    mock_sleep.assert_called_once_with(27.0)  # honours Retry-After from metadata


@patch("app.agent.generator.time.sleep")
@patch(_OAI_CLS_TARGET)
def test_openrouter_raises_after_max_retries(mock_oai_cls, mock_sleep, monkeypatch):
    """Three consecutive 429s should raise GeneratorError (not loop forever)."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "")   # ensure Groq path is taken
    get_settings.cache_clear()

    rate_err = _make_rate_limit_error(retry_after=15.0)
    create_mock = mock_oai_cls.return_value.chat.completions.create
    create_mock.side_effect = [rate_err, rate_err, rate_err]

    with pytest.raises(GeneratorError, match="rate-limited"):
        generate_dockerfile(_flask_profile())

    assert create_mock.call_count == 3         # tried 3 times
    assert mock_sleep.call_count == 2          # slept between attempts 1→2 and 2→3


@patch("app.agent.generator.time.sleep")
@patch(_OAI_CLS_TARGET)
def test_openrouter_falls_back_to_default_wait_if_no_retry_after(
    mock_oai_cls, mock_sleep, monkeypatch
):
    """If the 429 has no retry_after metadata the default back-off is used."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "")   # ensure Groq path is taken
    get_settings.cache_clear()

    import httpx
    from openai import RateLimitError

    # 429 with no retry_after in body or headers
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    bare_resp = httpx.Response(
        status_code=429,
        content=b'{"error": {"message": "rate limited", "code": 429, "metadata": {}}}',
        headers={"content-type": "application/json"},
        request=req,
    )
    bare_err = RateLimitError(
        "rate limited",
        response=bare_resp,
        body={"error": {"message": "rate limited", "code": 429, "metadata": {}}},
    )

    valid_json = _valid_output().model_dump_json()
    mock_choice = MagicMock()
    mock_choice.message.content = valid_json
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    create_mock = mock_oai_cls.return_value.chat.completions.create
    create_mock.side_effect = [bare_err, mock_response]

    result = generate_dockerfile(_flask_profile())

    assert isinstance(result, GeneratorOutput)
    # Default wait for attempt 0 is _RATE_LIMIT_DEFAULT_WAIT * (0+1) = 30
    mock_sleep.assert_called_once_with(30)


# ── GeneratorOutput Pydantic validators ───────────────────────────────────────


def test_dockerfile_must_start_with_from():
    with pytest.raises(Exception, match="FROM"):
        GeneratorOutput(
            dockerfile="RUN echo hello",
            run_command="docker run app",
            exposed_port=None,
            healthcheck=HealthCheck(type="exit", detail=""),
            reasoning="test",
        )


def test_dockerfile_must_not_be_empty():
    with pytest.raises(Exception):
        GeneratorOutput(
            dockerfile="   ",
            run_command="docker run app",
            exposed_port=None,
            healthcheck=HealthCheck(type="exit", detail=""),
            reasoning="test",
        )


def test_run_command_must_not_be_empty():
    with pytest.raises(Exception):
        GeneratorOutput(
            dockerfile="FROM scratch\n",
            run_command="",
            exposed_port=None,
            healthcheck=HealthCheck(type="exit", detail=""),
            reasoning="test",
        )
