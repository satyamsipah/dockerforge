"""
Unit tests for the builder module.

All tests mock subprocess.Popen so no Docker daemon is needed.
Tests cover: success path, failure path (error tail), timeout,
Docker-not-available, emit callback, .dockerignore writing.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agent.builder import (
    BuildResult,
    BuildTimeoutError,
    DockerNotAvailableError,
    build_image,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_popen_mock(lines: list[str], returncode: int) -> MagicMock:
    """Return a mock subprocess.Popen that yields *lines* and exits with *returncode*."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter(line + "\n" for line in lines)
    mock_proc.returncode = returncode
    # proc.wait(timeout=...) returns normally (no exception) → success
    mock_proc.wait.return_value = returncode
    return mock_proc


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp())


# ── Dockerfile + .dockerignore writing ───────────────────────────────────────


@patch("app.agent.builder.subprocess.Popen")
def test_dockerfile_written_to_context(mock_popen):
    mock_popen.return_value = _make_popen_mock(["Successfully built abc"], 0)
    ctx = _tmp_dir()
    build_image(ctx, "test-image", "FROM scratch\n")
    assert (ctx / "Dockerfile").read_text() == "FROM scratch\n"


@patch("app.agent.builder.subprocess.Popen")
def test_dockerignore_written_when_provided(mock_popen):
    mock_popen.return_value = _make_popen_mock([], 0)
    ctx = _tmp_dir()
    build_image(ctx, "test-image", "FROM scratch\n", dockerignore_content="*.pyc\n")
    assert (ctx / ".dockerignore").read_text() == "*.pyc\n"


@patch("app.agent.builder.subprocess.Popen")
def test_no_dockerignore_file_when_not_provided(mock_popen):
    mock_popen.return_value = _make_popen_mock([], 0)
    ctx = _tmp_dir()
    build_image(ctx, "test-image", "FROM scratch\n")
    assert not (ctx / ".dockerignore").exists()


# ── Success path ──────────────────────────────────────────────────────────────


@patch("app.agent.builder.subprocess.Popen")
def test_successful_build_returns_success_true(mock_popen):
    lines = [
        "Step 1/2 : FROM python:3.11-slim",
        " ---> abc123",
        "Step 2/2 : CMD echo hello",
        "Successfully built def456",
    ]
    mock_popen.return_value = _make_popen_mock(lines, returncode=0)
    result = build_image(_tmp_dir(), "app", "FROM python:3.11-slim\nCMD echo hello\n")
    assert result.success is True
    assert result.exit_code == 0
    assert result.error_tail == ""


@patch("app.agent.builder.subprocess.Popen")
def test_successful_build_returns_correct_image_tag(mock_popen):
    mock_popen.return_value = _make_popen_mock(["Successfully built abc"], 0)
    result = build_image(_tmp_dir(), "my-custom-tag", "FROM scratch\n")
    assert result.image_tag == "my-custom-tag"


@patch("app.agent.builder.subprocess.Popen")
def test_emit_called_for_each_line(mock_popen):
    lines = ["line1", "line2", "line3"]
    mock_popen.return_value = _make_popen_mock(lines, returncode=0)
    emitted: list[str] = []
    build_image(_tmp_dir(), "app", "FROM scratch\n", emit=emitted.append)
    assert emitted == lines


@patch("app.agent.builder.subprocess.Popen")
def test_log_lines_captured_in_result(mock_popen):
    lines = ["Step 1/1 : FROM scratch", "Successfully built abc123"]
    mock_popen.return_value = _make_popen_mock(lines, returncode=0)
    result = build_image(_tmp_dir(), "app", "FROM scratch\n")
    assert result.log_lines == lines


# ── Failure path ──────────────────────────────────────────────────────────────


@patch("app.agent.builder.subprocess.Popen")
def test_failed_build_returns_success_false(mock_popen):
    lines = ["Step 1/2 : FROM python:3.11-slim", "ERROR: package not found"]
    mock_popen.return_value = _make_popen_mock(lines, returncode=1)
    result = build_image(_tmp_dir(), "app", "FROM python:3.11-slim\nRUN bad\n")
    assert result.success is False
    assert result.exit_code == 1


@patch("app.agent.builder.subprocess.Popen")
def test_failed_build_error_tail_contains_last_lines(mock_popen):
    # Build fails; error_tail must contain the final output
    lines = ["step1", "step2", "ERROR: fatal error here"]
    mock_popen.return_value = _make_popen_mock(lines, returncode=1)
    result = build_image(_tmp_dir(), "app", "FROM scratch\n")
    assert "ERROR: fatal error here" in result.error_tail


@patch("app.agent.builder.subprocess.Popen")
def test_success_has_empty_error_tail(mock_popen):
    mock_popen.return_value = _make_popen_mock(["Successfully built abc"], 0)
    result = build_image(_tmp_dir(), "app", "FROM scratch\n")
    assert result.error_tail == ""


# ── Timeout ───────────────────────────────────────────────────────────────────


@patch("app.agent.builder.subprocess.Popen")
def test_timeout_raises_build_timeout_error(mock_popen):
    mock_proc = _make_popen_mock([], returncode=0)
    # side_effect as a list: first call (with timeout=) raises; second call
    # (the bare proc.wait() cleanup after proc.kill()) returns normally.
    mock_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd=["docker"], timeout=1),
        None,
    ]
    mock_popen.return_value = mock_proc

    with pytest.raises(BuildTimeoutError, match="timed out"):
        build_image(_tmp_dir(), "app", "FROM scratch\n", timeout_s=1)


# ── Docker not available ──────────────────────────────────────────────────────


@patch("app.agent.builder.subprocess.Popen")
def test_docker_not_on_path_raises(mock_popen):
    mock_popen.side_effect = FileNotFoundError("docker not found")
    with pytest.raises(DockerNotAvailableError, match="docker"):
        build_image(_tmp_dir(), "app", "FROM scratch\n")


@patch("app.agent.builder.subprocess.Popen")
def test_docker_daemon_not_running_raises(mock_popen):
    lines = ["Cannot connect to the Docker daemon at unix:///var/run/docker.sock"]
    mock_popen.return_value = _make_popen_mock(lines, returncode=1)
    with pytest.raises(DockerNotAvailableError, match="daemon"):
        build_image(_tmp_dir(), "app", "FROM scratch\n")


# ── docker build uses --no-cache ──────────────────────────────────────────────


@patch("app.agent.builder.subprocess.Popen")
def test_build_uses_no_cache_flag(mock_popen):
    mock_popen.return_value = _make_popen_mock([], 0)
    build_image(_tmp_dir(), "app", "FROM scratch\n")
    call_args = mock_popen.call_args
    cmd = call_args.args[0]
    assert "--no-cache" in cmd
