"""
API routes for DockerForge.

Phase 6 completes the SSE stream endpoint.  The generator:
  1. Picks up where the client left off via ``Last-Event-ID`` (safe reconnect).
  2. Drains all buffered events from the job store.
  3. Polls for new events (100 ms intervals) while the job is still running.
  4. Closes the stream once the job reaches ``done`` or ``failed``, or the
     client disconnects.

SSE is implemented with Starlette's built-in ``StreamingResponse``
(``text/event-stream`` media type) — no third-party library needed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl

from app import __version__
from app.agent.cloner import InvalidRepoURLError, validate_url
from app.agent.orchestrator import run_pipeline_background
from app.config import get_settings
from app.models.job import create_job, get_job

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# Healthcheck
# --------------------------------------------------------------------------- #

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@router.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness probe — confirms the API process is up and serving."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
    )


# --------------------------------------------------------------------------- #
# Forge — start a job
# --------------------------------------------------------------------------- #

class ForgeRequest(BaseModel):
    """Request body for starting a forge job."""
    repo_url: HttpUrl


class ForgeJobStarted(BaseModel):
    """Response when a job is successfully enqueued."""
    job_id: str
    status: str
    stream_url: str


@router.post("/forge", response_model=ForgeJobStarted, status_code=status.HTTP_202_ACCEPTED, tags=["forge"])
async def start_forge(
    body: ForgeRequest,
    background_tasks: BackgroundTasks,
) -> ForgeJobStarted:
    """
    Validate the GitHub repo URL, create a forge job, and kick off the
    pipeline in a background thread.

    Returns immediately with a ``job_id``.  Consume live events from
    ``GET /api/forge/{job_id}/stream`` or poll
    ``GET /api/forge/{job_id}`` for status.
    """
    url = str(body.repo_url)
    try:
        validate_url(url)
    except InvalidRepoURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job = create_job(url)
    background_tasks.add_task(run_pipeline_background, job.job_id)

    return ForgeJobStarted(
        job_id=job.job_id,
        status=job.status,
        stream_url=f"/api/forge/{job.job_id}/stream",
    )


# --------------------------------------------------------------------------- #
# Forge — inspect job
# --------------------------------------------------------------------------- #

class ForgeJobStatus(BaseModel):
    """Snapshot of a job's current state."""
    job_id: str
    repo_url: str
    status: str
    event_count: int
    output: dict | None


@router.get("/forge/{job_id}", response_model=ForgeJobStatus, tags=["forge"])
def get_forge_job(job_id: str) -> ForgeJobStatus:
    """Return the current status and event count for a job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return ForgeJobStatus(
        job_id=job.job_id,
        repo_url=job.repo_url,
        status=job.status,
        event_count=len(job.events),
        output=job.output,
    )


# --------------------------------------------------------------------------- #
# Forge — SSE stream
# --------------------------------------------------------------------------- #

@router.get("/forge/{job_id}/stream", tags=["forge"])
async def stream_forge(request: Request, job_id: str) -> StreamingResponse:
    """
    Server-Sent Events stream of typed job events.

    Event types: ``step_started``, ``log_line``, ``attempt``,
    ``build_result``, ``run_result``, ``done``, ``error``.

    Each SSE message carries a numeric ``id`` so clients can resume
    mid-stream after a reconnect via the ``Last-Event-ID`` header.

    The stream closes once the job reaches ``done`` or ``failed``.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    async def _event_stream() -> AsyncIterator[str]:
        # Resume support: start from the event *after* the last one received.
        try:
            sent = int(request.headers.get("last-event-id", "")) + 1
        except (ValueError, TypeError):
            sent = 0

        while True:
            # Drain all buffered events since last send.
            while sent < len(job.events):
                data = json.dumps(job.events[sent])
                yield f"id: {sent}\ndata: {data}\n\n"
                sent += 1

            # Close if the pipeline has finished.
            if job.status in ("done", "failed"):
                break

            # Job still running — wait, then check for client disconnect.
            await asyncio.sleep(0.1)
            if await request.is_disconnected():
                break

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering for live stream
            "Connection": "keep-alive",
        },
    )
