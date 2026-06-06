"""
Unit tests for the runner module.

All tests mock subprocess and socket so no Docker daemon is needed.
Tests cover: exit mode (success/failure/timeout), http mode, tcp mode,
log mode, container start failure, cleanup guarantee, emit callback,
and resource-limit flags.
"""

from __future__ import annotations

import socket
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from app.agent.runner import RunError, RunResult, run_and_verify


# ── Helpers ───────────────────────────────────────────────────────────────────

_IMAGE = "dockerforge-test"
_BASE = dict(
    image_tag=_IMAGE,
    healthcheck_type="exit",
    healthcheck_detail="0",
    container_port=None,
    container_name="dockerforge-testcontainer",
    timeout_s=10,
    memory_limit="256m",
    cpus_limit="0.5",
)


def _make_run_mock(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ── Exit mode ─────────────────────────────────────────────────────────────────


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_success(mock_run):
    mock_run.return_value = _make_run_mock(0, stdout="done")
    result = run_and_verify(**_BASE)
    assert result.success is True
    assert result.container_id == ""


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_nonzero_raises(mock_run):
    mock_run.return_value = _make_run_mock(1, stderr="error output")
    with pytest.raises(RunError, match="exited with code 1"):
        run_and_verify(**_BASE)


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_expected_nonzero_passes(mock_run):
    mock_run.return_value = _make_run_mock(2, stdout="expected")
    kwargs = {**_BASE, "healthcheck_detail": "2"}
    result = run_and_verify(**kwargs)
    assert result.success is True


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_timeout_raises(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["docker"], timeout=10)
    with pytest.raises(RunError, match="timed out"):
        run_and_verify(**_BASE)


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_docker_missing_raises(mock_run):
    mock_run.side_effect = FileNotFoundError("docker not found")
    with pytest.raises(RunError, match="docker"):
        run_and_verify(**_BASE)


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_passes_memory_and_cpus_flags(mock_run):
    mock_run.return_value = _make_run_mock(0)
    run_and_verify(**_BASE)
    cmd = mock_run.call_args.args[0]
    assert "--memory" in cmd
    assert "256m" in cmd
    assert "--cpus" in cmd
    assert "0.5" in cmd


@patch("app.agent.runner.subprocess.run")
def test_exit_mode_emit_called(mock_run):
    mock_run.return_value = _make_run_mock(0, stdout="hello")
    lines: list[str] = []
    run_and_verify(**_BASE, emit_log=lines.append)
    assert any("hello" in line for line in lines)


# ── HTTP mode ─────────────────────────────────────────────────────────────────


@patch("app.agent.runner._stop_and_remove")
@patch("app.agent.runner._docker_logs", return_value="Server started")
@patch("app.agent.runner._verify_http")
@patch("app.agent.runner._start_detached", return_value="abc123")
def test_http_mode_success(mock_start, mock_verify, mock_logs, mock_stop):
    kwargs = {**_BASE, "healthcheck_type": "http", "healthcheck_detail": "/", "container_port": 8000}
    result = run_and_verify(**kwargs)
    assert result.success is True
    mock_verify.assert_called_once()
    mock_stop.assert_called_once()
    assert mock_stop.call_args.args[0] == "abc123"


@patch("app.agent.runner._stop_and_remove")
@patch("app.agent.runner._verify_http", side_effect=RunError("HTTP timeout"))
@patch("app.agent.runner._start_detached", return_value="abc123")
def test_http_mode_timeout_raises_and_cleans_up(mock_start, mock_verify, mock_stop):
    kwargs = {**_BASE, "healthcheck_type": "http", "healthcheck_detail": "/", "container_port": 8000}
    with pytest.raises(RunError, match="HTTP timeout"):
        run_and_verify(**kwargs)
    mock_stop.assert_called_once()


# ── TCP mode ──────────────────────────────────────────────────────────────────


@patch("app.agent.runner._stop_and_remove")
@patch("app.agent.runner._docker_logs", return_value="")
@patch("app.agent.runner._verify_tcp")
@patch("app.agent.runner._start_detached", return_value="cid456")
def test_tcp_mode_success(mock_start, mock_verify, mock_logs, mock_stop):
    kwargs = {**_BASE, "healthcheck_type": "tcp", "healthcheck_detail": "3000", "container_port": 3000}
    result = run_and_verify(**kwargs)
    assert result.success is True
    mock_verify.assert_called_once()


@patch("app.agent.runner._stop_and_remove")
@patch("app.agent.runner._verify_tcp", side_effect=RunError("TCP timeout"))
@patch("app.agent.runner._start_detached", return_value="cid456")
def test_tcp_mode_failure_cleans_up(mock_start, mock_verify, mock_stop):
    kwargs = {**_BASE, "healthcheck_type": "tcp", "healthcheck_detail": "3000", "container_port": 3000}
    with pytest.raises(RunError):
        run_and_verify(**kwargs)
    mock_stop.assert_called_once()


# ── Log mode ──────────────────────────────────────────────────────────────────


@patch("app.agent.runner._stop_and_remove")
@patch("app.agent.runner._docker_logs", return_value="Listening on port 5000")
@patch("app.agent.runner._verify_log")
@patch("app.agent.runner._start_detached", return_value="cid789")
def test_log_mode_success(mock_start, mock_verify, mock_logs, mock_stop):
    kwargs = {**_BASE, "healthcheck_type": "log", "healthcheck_detail": "Listening on"}
    result = run_and_verify(**kwargs)
    assert result.success is True
    mock_verify.assert_called_once()


# ── Container start failure ───────────────────────────────────────────────────


@patch("app.agent.runner.subprocess.run")
def test_start_detached_failure_raises(mock_run):
    mock_run.return_value = _make_run_mock(125, stderr="image not found")
    kwargs = {**_BASE, "healthcheck_type": "http", "healthcheck_detail": "/", "container_port": 8000}
    with pytest.raises(RunError, match="Failed to start container"):
        run_and_verify(**kwargs)


# ── Unknown healthcheck type ──────────────────────────────────────────────────


@patch("app.agent.runner._stop_and_remove")
@patch("app.agent.runner._start_detached", return_value="cid")
def test_unknown_healthcheck_type_raises(mock_start, mock_stop):
    kwargs = {**_BASE, "healthcheck_type": "ftp", "healthcheck_detail": "something"}
    with pytest.raises(RunError, match="Unknown healthcheck type"):
        run_and_verify(**kwargs)
    mock_stop.assert_called_once()
