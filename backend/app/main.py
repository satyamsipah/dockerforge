"""
DockerForge FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload --port 8000

Then hit http://localhost:8000/api/health (or /docs for the OpenAPI UI).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router as api_router
from app.config import get_settings


def create_app() -> FastAPI:
    """Application factory — builds and configures the FastAPI instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="An AI agent that forges working Dockerfiles for GitHub repos.",
    )

    # The frontend is a separate origin (Vite dev server), so CORS is required
    # for the browser to call this API and consume the SSE stream.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        """Friendly root pointer to the docs and health endpoint."""
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "health": "/api/health",
        }

    return app


app = create_app()
