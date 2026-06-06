"""
API routes for DockerForge.

Phase 1 ships a working healthcheck. The `/api/forge` endpoints are declared
here as explicit placeholders (HTTP 501) so the API contract is visible from
day one and the frontend can be wired against stable URLs; their real
implementations arrive in later phases (orchestrator in Phase 4–5, SSE in
Phase 6).
"""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from app import __version__
from app.config import get_settings

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
# Forge (placeholders — implemented in later phases)
# --------------------------------------------------------------------------- #
class ForgeRequest(BaseModel):
    """Request body for starting a forge job."""

    repo_url: HttpUrl


@router.post("/forge", status_code=status.HTTP_501_NOT_IMPLEMENTED, tags=["forge"])
def start_forge(_: ForgeRequest) -> JSONResponse:
    """
    Validate a repo URL and kick off a forge job, returning a `job_id`.

    Not implemented in Phase 1 — the agent orchestrator is built in Phases 2–5.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"detail": "Forge pipeline not implemented yet (arrives in Phase 4–5)."},
    )


@router.get(
    "/forge/{job_id}/stream",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    tags=["forge"],
)
def stream_forge(job_id: str) -> JSONResponse:
    """
    Server-Sent Events stream of typed job events
    (`step_started`, `log_line`, `attempt`, `build_result`, `run_result`,
    `done`, `error`).

    Not implemented in Phase 1 — SSE streaming is wired up in Phase 6.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"detail": f"SSE stream for job {job_id} not implemented yet (arrives in Phase 6)."},
    )
