"""
Integration tests for the API routes.

Uses FastAPI's TestClient (synchronous ASGI wrapper) so no running server is
needed.  The pipeline background task is always mocked to a no-op so no
Docker daemon or Gemini key is required.

Tests cover:
  - Health endpoint
  - POST /forge: URL validation, 202 response shape
  - GET /forge/{job_id}: not-found, pending status
  - GET /forge/{job_id}/stream: not-found, SSE event format, resume via Last-Event-ID
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.job import clear_all_jobs, create_job


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_jobs():
    """Ensure the in-memory job store is empty before and after every test."""
    clear_all_jobs()
    yield
    clear_all_jobs()


@pytest.fixture
def client():
    return TestClient(create_app())


# ── Health ────────────────────────────────────────────────────────────────────


def test_health_returns_200(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_health_body_has_ok_status(client):
    resp = client.get("/api/health")
    assert resp.json()["status"] == "ok"


# ── POST /api/forge ───────────────────────────────────────────────────────────


def test_post_forge_invalid_url_returns_422(client):
    resp = client.post("/api/forge", json={"repo_url": "not-a-url"})
    assert resp.status_code == 422


def test_post_forge_non_github_url_returns_422(client):
    resp = client.post("/api/forge", json={"repo_url": "https://gitlab.com/owner/repo"})
    assert resp.status_code == 422


@patch("app.api.routes.run_pipeline_background", new_callable=AsyncMock)
def test_post_forge_valid_url_returns_202(mock_run, client):
    resp = client.post("/api/forge", json={"repo_url": "https://github.com/owner/repo"})
    assert resp.status_code == 202


@patch("app.api.routes.run_pipeline_background", new_callable=AsyncMock)
def test_post_forge_response_has_job_id_and_stream_url(mock_run, client):
    resp = client.post("/api/forge", json={"repo_url": "https://github.com/owner/repo"})
    data = resp.json()
    assert "job_id" in data
    assert data["stream_url"].startswith("/api/forge/")
    assert data["stream_url"].endswith("/stream")


# ── GET /api/forge/{job_id} ───────────────────────────────────────────────────


def test_get_job_not_found_returns_404(client):
    resp = client.get("/api/forge/nonexistent-id")
    assert resp.status_code == 404


@patch("app.api.routes.run_pipeline_background", new_callable=AsyncMock)
def test_get_job_returns_status_after_post(mock_run, client):
    post_resp = client.post("/api/forge", json={"repo_url": "https://github.com/owner/repo"})
    job_id = post_resp.json()["job_id"]
    get_resp = client.get(f"/api/forge/{job_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["job_id"] == job_id
    assert data["status"] in ("pending", "running")


# ── GET /api/forge/{job_id}/stream ────────────────────────────────────────────


def test_stream_not_found_returns_404(client):
    resp = client.get("/api/forge/nonexistent-id/stream")
    assert resp.status_code == 404


def test_stream_done_job_returns_200_with_event_stream_content(client):
    job = create_job("https://github.com/test/repo")
    job.status = "done"
    job.events = [{"type": "done", "dockerfile": "FROM scratch\n", "attempts": 1}]
    resp = client.get(f"/api/forge/{job.job_id}/stream")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_stream_done_job_yields_all_events(client):
    job = create_job("https://github.com/test/repo")
    job.status = "done"
    job.events = [
        {"type": "step_started", "step": "clone"},
        {"type": "done", "dockerfile": "FROM scratch\n", "attempts": 1},
    ]
    resp = client.get(f"/api/forge/{job.job_id}/stream")
    text = resp.text
    # Each event should appear in the stream body
    assert '"step_started"' in text
    assert '"done"' in text


def test_stream_events_have_id_prefix(client):
    job = create_job("https://github.com/test/repo")
    job.status = "done"
    job.events = [{"type": "done", "attempts": 1}]
    resp = client.get(f"/api/forge/{job.job_id}/stream")
    assert "id: 0" in resp.text


def test_stream_resume_via_last_event_id(client):
    job = create_job("https://github.com/test/repo")
    job.status = "done"
    job.events = [
        {"type": "step_started", "step": "clone"},
        {"type": "step_started", "step": "analyze"},
        {"type": "done", "attempts": 1},
    ]
    # Resume from after event 1 — should only get event 2
    resp = client.get(
        f"/api/forge/{job.job_id}/stream",
        headers={"Last-Event-ID": "1"},
    )
    text = resp.text
    assert '"done"' in text
    # Event 0 (clone) and event 1 (analyze) should NOT appear since we resumed at 2
    assert "clone" not in text
    assert '"analyze"' not in text


def test_stream_failed_job_closes_stream(client):
    job = create_job("https://github.com/test/repo")
    job.status = "failed"
    job.events = [{"type": "error", "message": "Build failed"}]
    resp = client.get(f"/api/forge/{job.job_id}/stream")
    assert resp.status_code == 200
    assert '"error"' in resp.text
