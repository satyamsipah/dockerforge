"""
In-memory job store for forge pipeline jobs.

A ForgeJob tracks the full lifecycle of one pipeline run — status, accumulated
typed events (streamed to the UI in Phase 6), and the final output.

The module-level ``_jobs`` dict is deliberately simple: it works for a
single-process server and makes jobs trivially inspectable in tests.  A
multi-worker production deployment would replace this with Redis or a DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ForgeJob:
    job_id: str
    repo_url: str

    # State machine: pending → running → done | failed
    status: str = "pending"

    # All orchestrator events accumulate here.
    # Phase 6's SSE endpoint reads from this list and streams it.
    events: list[dict[str, Any]] = field(default_factory=list)

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    # On success: the GeneratorOutput serialised as a dict.
    output: dict[str, Any] | None = None

    def append_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)


# ── Module-level store ────────────────────────────────────────────────────────

_jobs: dict[str, ForgeJob] = {}


def create_job(repo_url: str) -> ForgeJob:
    """Create a new pending job and register it in the store."""
    job_id = uuid.uuid4().hex
    job = ForgeJob(job_id=job_id, repo_url=repo_url)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> ForgeJob | None:
    """Return the job or ``None`` if not found."""
    return _jobs.get(job_id)


def clear_all_jobs() -> None:
    """Reset the store — used by tests to ensure isolation."""
    _jobs.clear()
