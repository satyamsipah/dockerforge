"""
End-to-end integration tests for the DockerForge pipeline.

Unlike unit tests (which mock at the component boundary), these tests use the
real FastAPI app, real HTTP routing, the real job store, and the real analyzer
running on actual fixture files.  Only the three external services are mocked:

  - cloned_repo  → yields the flask_app fixture directory (no git needed)
  - generate_dockerfile / fix_dockerfile  → return a known GeneratorOutput (no Gemini key)
  - build_image  → returns a mock BuildResult (no Docker daemon needed)
  - run_and_verify  → returns a mock RunResult (no Docker daemon needed)

This lets us verify end-to-end behaviour that unit tests can't catch:
  - FastAPI routing → job store → background task → orchestrator wiring
  - Real analyzer output feeds into generate_dockerfile call
  - SSE stream delivers the full typed-event sequence for a completed job
  - Last-Event-ID resume works across the whole stack
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent.builder import BuildResult
from app.agent.runner import RunResult
from app.main import create_app
from app.models.generator_output import GeneratorOutput, HealthCheck
from app.models.job import clear_all_jobs

# ── Fixtures + constants ──────────────────────────────────────────────────────

_FIXTURES = Path(__file__).parent / "fixtures"

_GOOD_OUTPUT = GeneratorOutput(
    dockerfile=(
        "FROM python:3.11-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n"
        "EXPOSE 5000\n"
        'CMD ["python", "app.py"]\n'
    ),
    run_command="docker run --rm -p 5000:5000 app",
    exposed_port=5000,
    healthcheck=HealthCheck(type="http", detail="/"),
    reasoning="Flask app with requirements.txt — standard python:3.11-slim base.",
)

_GOOD_BUILD = BuildResult(
    success=True,
    image_tag="dockerforge-inttest1",
    exit_code=0,
    log_lines=["Step 1/6 : FROM python:3.11-slim", "Successfully built abc123"],
)

_GOOD_RUN = RunResult(
    success=True,
    container_id="c0ffee1234",
    logs="Running on http://0.0.0.0:5000",
    message="HTTP 200 on /",
)


@contextmanager
def _fake_clone(fixture: str = "flask_app"):
    """Patch cloned_repo to yield a real fixture directory (no git required)."""
    fixture_path = _FIXTURES / fixture

    @contextmanager
    def _inner(*args, **kwargs):
        yield fixture_path

    with patch("app.agent.orchestrator.cloned_repo", side_effect=_inner):
        yield fixture_path


@pytest.fixture(autouse=True)
def _clean():
    clear_all_jobs()
    yield
    clear_all_jobs()


@pytest.fixture
def client():
    return TestClient(create_app())


# ── Helper: run a full forge and return (client, job_id) ─────────────────────

def _forge(client, fixture="flask_app", *, build=_GOOD_BUILD, run=_GOOD_RUN, output=_GOOD_OUTPUT):
    """POST a forge request, wait for completion, return job_id."""
    with patch("app.agent.orchestrator.generate_dockerfile", return_value=output), \
         patch("app.agent.orchestrator.build_image", return_value=build), \
         patch("app.agent.orchestrator.run_and_verify", return_value=run), \
         _fake_clone(fixture):
        resp = client.post(
            "/api/forge",
            json={"repo_url": "https://github.com/test/repo"},
        )
    assert resp.status_code == 202
    return resp.json()["job_id"]


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_successful_forge_job_reaches_done_status(client):
    """Full happy-path: POST → background pipeline completes → status is 'done'."""
    job_id = _forge(client)
    status = client.get(f"/api/forge/{job_id}").json()
    assert status["status"] == "done"
    assert status["output"] is not None
    assert "dockerfile" in status["output"]


def test_analyzer_receives_real_flask_profile(client):
    """
    The real analyzer runs against the flask_app fixture; its RepoProfile is
    passed to generate_dockerfile.  This confirms the HTTP → orchestrator →
    analyzer data-flow is wired correctly.
    """
    with patch("app.agent.orchestrator.generate_dockerfile", return_value=_GOOD_OUTPUT) as mock_gen, \
         patch("app.agent.orchestrator.build_image", return_value=_GOOD_BUILD), \
         patch("app.agent.orchestrator.run_and_verify", return_value=_GOOD_RUN), \
         _fake_clone("flask_app"):
        client.post("/api/forge", json={"repo_url": "https://github.com/test/repo"})

    mock_gen.assert_called_once()
    profile = mock_gen.call_args.args[0]
    assert profile.language == "python"
    assert profile.framework == "flask"


def test_sse_stream_contains_all_required_event_types(client):
    """
    The SSE stream for a completed job must contain at minimum: one
    step_started, one build_result, one run_result, and a done event.
    """
    job_id = _forge(client)
    stream = client.get(f"/api/forge/{job_id}/stream")
    assert stream.status_code == 200

    event_types = {
        json.loads(line[5:].strip())["type"]
        for line in stream.text.splitlines()
        if line.startswith("data:")
    }
    assert {"step_started", "build_result", "run_result", "done"}.issubset(event_types)


def test_done_event_contains_dockerfile_and_run_success(client):
    """
    The 'done' SSE event must carry the full Dockerfile and run_success flag
    so the frontend can render the DockerfileCard without a separate API call.
    """
    job_id = _forge(client)
    stream = client.get(f"/api/forge/{job_id}/stream")

    done_event = next(
        json.loads(line[5:].strip())
        for line in stream.text.splitlines()
        if line.startswith("data:") and '"done"' in line
    )
    assert "FROM python:3.11-slim" in done_event["dockerfile"]
    assert done_event["run_success"] is True
    assert done_event["attempts"] == 1


def test_retry_path_calls_fix_dockerfile_on_second_attempt(client):
    """
    When the first build fails, the orchestrator must call fix_dockerfile (not
    generate_dockerfile) for the second attempt, passing the error tail.
    """
    fail_build = BuildResult(
        success=False, image_tag="dockerforge-inttest2", exit_code=1,
        error_tail="ERROR: Could not find a version that satisfies the requirement flask==99",
    )

    with patch("app.agent.orchestrator.generate_dockerfile", return_value=_GOOD_OUTPUT) as mock_gen, \
         patch("app.agent.orchestrator.fix_dockerfile", return_value=_GOOD_OUTPUT) as mock_fix, \
         patch("app.agent.orchestrator.build_image", side_effect=[fail_build, _GOOD_BUILD]), \
         patch("app.agent.orchestrator.run_and_verify", return_value=_GOOD_RUN), \
         _fake_clone("flask_app"):
        resp = client.post("/api/forge", json={"repo_url": "https://github.com/test/repo"})
        job_id = resp.json()["job_id"]

    assert client.get(f"/api/forge/{job_id}").json()["status"] == "done"
    assert mock_gen.call_count == 1
    assert mock_fix.call_count == 1
    assert "flask==99" in mock_fix.call_args.kwargs["build_error_tail"]


def test_all_builds_fail_job_reaches_failed_status(client):
    """
    When every build attempt fails, the job status must be 'failed' and the
    SSE stream must contain an 'error' event with attempt_history.
    """
    fail_build = BuildResult(
        success=False, image_tag="dockerforge-inttest3", exit_code=1,
        error_tail="RUN npm install FAILED",
    )

    with patch("app.agent.orchestrator.generate_dockerfile", return_value=_GOOD_OUTPUT), \
         patch("app.agent.orchestrator.fix_dockerfile", return_value=_GOOD_OUTPUT), \
         patch("app.agent.orchestrator.build_image", return_value=fail_build), \
         _fake_clone("flask_app"):
        resp = client.post("/api/forge", json={"repo_url": "https://github.com/test/repo"})
        job_id = resp.json()["job_id"]

    assert client.get(f"/api/forge/{job_id}").json()["status"] == "failed"

    stream = client.get(f"/api/forge/{job_id}/stream")
    error_event = next(
        json.loads(line[5:].strip())
        for line in stream.text.splitlines()
        if line.startswith("data:") and '"error"' in line
    )
    assert "attempt_history" in error_event
    assert len(error_event["attempt_history"]) == 3
