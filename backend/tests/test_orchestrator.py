"""
Unit tests for the orchestrator's pipeline logic.

These tests target ``_run_pipeline`` directly (not the job-store wrapper)
so they can inject a plain list as the emit collector and mock all
external calls (clone, analyze, generate, build, run) without a job store,
Docker daemon, git, or Gemini API key.

Key scenarios:
  - Success on first attempt (with run+verify)
  - Success on second attempt (retry after one failure)
  - All 3 attempts fail → "error" event with full attempt history
  - Clone failure → "error" event, no build attempted
  - GeneratorError → "error" event, no build attempted
  - fix_dockerfile called (not generate_dockerfile) on attempts 2+
  - Event sequence is correct (step ordering, attempt numbering)
  - RunError does NOT fail the job (best-effort; "done" still emitted)
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agent.orchestrator import _run_pipeline
from app.agent.builder import BuildResult
from app.agent.cloner import CloneError, InvalidRepoURLError
from app.agent.generator import GeneratorError
from app.agent.runner import RunError, RunResult
from app.models.generator_output import GeneratorOutput, HealthCheck


# ── Constants / helpers ────────────────────────────────────────────────────────

_URL = "https://github.com/test/repo"
_JOB_ID = "abcdef01234567890"
_BASE_KWARGS = dict(
    max_attempts=3,
    clone_timeout_s=60,
    max_repo_mb=200,
    build_timeout_s=600,
    run_timeout_s=60,
    container_memory="256m",
    container_cpus="0.5",
    job_id=_JOB_ID,
)


def _good_output(reasoning: str = "good") -> GeneratorOutput:
    return GeneratorOutput(
        dockerfile="FROM python:3.11-slim\nCMD echo hi\n",
        compose=None,
        run_command="docker run --rm app",
        exposed_port=5000,
        healthcheck=HealthCheck(type="http", detail="/"),
        reasoning=reasoning,
    )


def _good_build(tag: str = "dockerforge-abcdef01") -> BuildResult:
    return BuildResult(success=True, image_tag=tag, exit_code=0)


def _bad_build(tag: str = "dockerforge-abcdef01", error: str = "RUN failed") -> BuildResult:
    return BuildResult(success=False, image_tag=tag, exit_code=1, error_tail=error)


def _good_run() -> RunResult:
    return RunResult(success=True, container_id="abc123", logs="Server started", message="HTTP 200")


def _collect_events(repo_url: str = _URL, **kwargs) -> list[dict]:
    events: list[dict] = []
    merged = {**_BASE_KWARGS, **kwargs}
    _run_pipeline(repo_url, events.append, **merged)
    return events


def _event_types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# ── Mocking helpers ───────────────────────────────────────────────────────────

@contextmanager
def _mock_clone(path: Path = Path("/tmp/fake")):
    """Patch cloned_repo to yield a fake path without any real git operation."""
    @contextmanager
    def _fake_cloned_repo(*args, **kwargs):
        yield path

    with patch("app.agent.orchestrator.cloned_repo", side_effect=_fake_cloned_repo):
        yield path


# ── Happy path — success on first attempt ─────────────────────────────────────


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_success_first_attempt(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()

    types = _event_types(events)
    assert "done" in types
    assert "error" not in types


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_success_returns_output_dict(mock_analyze, mock_gen, mock_build, mock_run):
    events: list[dict] = []
    with _mock_clone():
        result = _run_pipeline(_URL, events.append, **_BASE_KWARGS)
    assert result is not None
    assert "dockerfile" in result


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_success_emits_correct_step_order(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()

    step_events = [e for e in events if e["type"] == "step_started"]
    steps = [e["step"] for e in step_events]
    assert steps == ["clone", "analyze", "generate", "build", "run"]


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_success_first_attempt_number_is_one(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()
    attempt_events = [e for e in events if e["type"] == "attempt"]
    assert len(attempt_events) == 1
    assert attempt_events[0]["number"] == 1


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_done_event_includes_run_success(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()
    done = next(e for e in events if e["type"] == "done")
    assert done["run_success"] is True


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_run_result_event_emitted(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()
    assert "run_result" in _event_types(events)


# ── RunError is best-effort — job still succeeds ──────────────────────────────


@patch("app.agent.orchestrator.run_and_verify", side_effect=RunError("port not open"))
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_run_error_does_not_fail_job(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()
    types = _event_types(events)
    assert "done" in types
    assert "error" not in types


@patch("app.agent.orchestrator.run_and_verify", side_effect=RunError("port not open"))
@patch("app.agent.orchestrator.build_image", return_value=_good_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_run_error_done_has_run_success_false(mock_analyze, mock_gen, mock_build, mock_run):
    with _mock_clone():
        events = _collect_events()
    done = next(e for e in events if e["type"] == "done")
    assert done["run_success"] is False


# ── Retry — success on second attempt ────────────────────────────────────────


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output("fixed"))
@patch("app.agent.orchestrator.build_image")
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output("initial"))
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_retry_succeeds_on_second_attempt(mock_analyze, mock_gen, mock_build, mock_fix, mock_run):
    mock_build.side_effect = [_bad_build(), _good_build()]

    with _mock_clone():
        events = _collect_events()

    types = _event_types(events)
    assert "done" in types
    assert "error" not in types


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output("fixed"))
@patch("app.agent.orchestrator.build_image")
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output("initial"))
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_retry_calls_fix_not_generate_on_attempt_2(mock_analyze, mock_gen, mock_build, mock_fix, mock_run):
    mock_build.side_effect = [_bad_build(), _good_build()]

    with _mock_clone():
        _collect_events()

    assert mock_gen.call_count == 1
    assert mock_fix.call_count == 1


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output("fixed"))
@patch("app.agent.orchestrator.build_image")
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output("initial"))
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_retry_passes_error_tail_to_fix(mock_analyze, mock_gen, mock_build, mock_fix, mock_run):
    error_msg = "Could not find package flask"
    mock_build.side_effect = [_bad_build(error=error_msg), _good_build()]

    with _mock_clone():
        _collect_events()

    fix_call_kwargs = mock_fix.call_args
    assert fix_call_kwargs.kwargs["build_error_tail"] == error_msg
    assert fix_call_kwargs.kwargs["attempt_number"] == 2


@patch("app.agent.orchestrator.run_and_verify", return_value=_good_run())
@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output("fixed"))
@patch("app.agent.orchestrator.build_image")
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output("initial"))
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_retry_emits_two_attempt_events(mock_analyze, mock_gen, mock_build, mock_fix, mock_run):
    mock_build.side_effect = [_bad_build(), _good_build()]

    with _mock_clone():
        events = _collect_events()

    attempt_events = [e for e in events if e["type"] == "attempt"]
    assert len(attempt_events) == 2
    assert [e["number"] for e in attempt_events] == [1, 2]


# ── All attempts exhausted ────────────────────────────────────────────────────


@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.build_image", return_value=_bad_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_all_attempts_fail_emits_error(mock_analyze, mock_gen, mock_build, mock_fix):
    with _mock_clone():
        events = _collect_events()

    types = _event_types(events)
    assert "error" in types
    assert "done" not in types


@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.build_image", return_value=_bad_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_all_attempts_fail_returns_none(mock_analyze, mock_gen, mock_build, mock_fix):
    events: list[dict] = []
    with _mock_clone():
        result = _run_pipeline(_URL, events.append, **_BASE_KWARGS)
    assert result is None


@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.build_image", return_value=_bad_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_all_attempts_fail_error_has_attempt_history(mock_analyze, mock_gen, mock_build, mock_fix):
    with _mock_clone():
        events = _collect_events()

    error_event = next(e for e in events if e["type"] == "error")
    assert "attempt_history" in error_event
    assert len(error_event["attempt_history"]) == 3


@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.build_image", return_value=_bad_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_all_three_attempts_generate_attempt_events(mock_analyze, mock_gen, mock_build, mock_fix):
    with _mock_clone():
        events = _collect_events()

    attempt_numbers = [e["number"] for e in events if e["type"] == "attempt"]
    assert attempt_numbers == [1, 2, 3]


# ── Error paths — clone / generator failures ──────────────────────────────────


def test_clone_failure_emits_error():
    with patch("app.agent.orchestrator.cloned_repo", side_effect=CloneError("network error")):
        events = _collect_events()
    error = next(e for e in events if e["type"] == "error")
    assert "Clone failed" in error["message"]


def test_invalid_url_emits_error():
    with patch("app.agent.orchestrator.cloned_repo", side_effect=InvalidRepoURLError("bad url")):
        events = _collect_events()
    error = next(e for e in events if e["type"] == "error")
    assert "Invalid repo URL" in error["message"]


@patch("app.agent.orchestrator.build_image")
@patch("app.agent.orchestrator.generate_dockerfile", side_effect=GeneratorError("API quota exceeded"))
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_generator_error_emits_error(mock_analyze, mock_gen, mock_build):
    with _mock_clone():
        events = _collect_events()
    error = next(e for e in events if e["type"] == "error")
    assert "LLM generation failed" in error["message"]
    mock_build.assert_not_called()


# ── Respects max_attempts setting ────────────────────────────────────────────


@patch("app.agent.orchestrator.fix_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.build_image", return_value=_bad_build())
@patch("app.agent.orchestrator.generate_dockerfile", return_value=_good_output())
@patch("app.agent.orchestrator.analyze_repo", return_value=MagicMock())
def test_max_attempts_one_means_no_retry(mock_analyze, mock_gen, mock_build, mock_fix):
    with _mock_clone():
        events = _collect_events(max_attempts=1)

    assert mock_gen.call_count == 1
    assert mock_fix.call_count == 0
    assert "error" in _event_types(events)
