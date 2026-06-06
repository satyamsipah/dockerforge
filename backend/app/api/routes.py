"""
API routes for DockerForge.

Phase 4 implements POST /api/forge (starts a pipeline job) and
GET /api/forge/{job_id} (job status + events).
GET /api/forge/{job_id}/stream (SSE) remains a 501 stub until Phase 6.
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import JSONResponse
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

    Returns immediately with a ``job_id``.  Poll ``GET /api/forge/{job_id}``
    for status, or consume the SSE stream at ``GET /api/forge/{job_id}/stream``
    (available from Phase 6).
    """
    url = str(body.repo_url)
    try:
        validate_url(url)
    except InvalidRepoURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job = create_job(url)
    # run_pipeline_background uses asyncio.to_thread internally, so the
    # blocking subprocess calls never touch the event loop.
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
    """
    Return the current status and event count for a job.

    Useful for polling and for integration tests.  The full event list
    (and the live stream) are exposed via the SSE endpoint in Phase 6.
    """
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
# Forge — SSE stream (Phase 6 stub)
# --------------------------------------------------------------------------- #

@router.get(
    "/forge/{job_id}/stream",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    tags=["forge"],
)
def stream_forge(job_id: str) -> JSONResponse:
    """
    Server-Sent Events stream of typed job events
    (``step_started``, ``log_line``, ``attempt``, ``build_result``,
    ``run_result``, ``done``, ``error``).

    SSE wiring is implemented in Phase 6.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"detail": f"SSE stream for job '{job_id}' arrives in Phase 6."},
    )
