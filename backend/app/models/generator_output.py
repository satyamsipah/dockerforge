"""
GeneratorOutput — validated schema for a single Gemini generation call.

Why structured output / JSON mode instead of asking the model to produce a
Dockerfile in freeform prose?
  - Reliability: JSON mode guarantees the response matches the schema.  Parsing
    a Dockerfile out of a markdown code block with regex is fragile and fails on
    any unexpected formatting.
  - Downstream contracts: the builder (Phase 4) and runner (Phase 5) need the
    `run_command`, `exposed_port`, and `healthcheck` fields as typed values —
    not something we have to scrape out of paragraphs.
  - Retry context: when a build fails, we send the model back the full
    GeneratorOutput from the previous attempt so it can reason about a targeted
    fix (Phase 4).  A typed object makes that context construction clean.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HealthCheck(BaseModel):
    """Describes how the runner (Phase 5) should verify the running container."""

    # http  → poll an HTTP endpoint until 2xx/3xx
    # tcp   → check that the port accepts a TCP connection
    # log   → wait for a known substring in container logs
    # exit  → CLI tool: verify zero exit code / expected stdout
    type: Literal["http", "tcp", "log", "exit"]

    detail: str = Field(
        description=(
            "For 'http': URL path (e.g. '/health' or '/'). "
            "For 'log': substring to wait for (e.g. 'Listening on'). "
            "For 'tcp': leave empty. "
            "For 'exit': expected output substring or empty."
        )
    )


class GeneratorOutput(BaseModel):
    """
    Validated output from one Gemini generation call.

    This schema is passed directly to Gemini as ``response_schema``, which
    means the SDK enforces it before we ever see the response.  The
    ``field_validator`` on ``dockerfile`` is a second line of defence.
    """

    # Full Dockerfile content — the main deliverable.
    dockerfile: str

    # docker-compose.yml when the app needs multiple services; null otherwise.
    compose: str | None = None

    # ``docker run`` flags + image name for the runner to use.
    # Example: "docker run --rm -p 5000:5000 dockerforge-app"
    run_command: str

    # Port the container exposes (echoes what's in the Dockerfile EXPOSE).
    exposed_port: int | None = None

    # How Phase 5 should verify the container is healthy.
    healthcheck: HealthCheck

    # .dockerignore content (keeps build context tight).
    dockerignore: str | None = None

    # One-paragraph explanation of the key decisions (base image, stage split,
    # non-root user, etc.) — shown in the UI so the user understands the output.
    reasoning: str

    @field_validator("dockerfile")
    @classmethod
    def dockerfile_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Dockerfile must not be empty")
        if not v.strip().upper().startswith("FROM"):
            raise ValueError("Dockerfile must start with a FROM instruction")
        return v

    @field_validator("run_command")
    @classmethod
    def run_command_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("run_command must not be empty")
        return v
