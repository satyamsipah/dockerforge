"""
Forge pipeline orchestrator — the agent loop.

Entry point: ``run_pipeline_sync(job_id)`` or the async wrapper
``run_pipeline_background(job_id)`` for FastAPI background tasks.

The loop:

    clone → analyze → generate ──→ build
                                    │
                            success?─┤
                                 yes │
                                     ▼
                                 run+verify → done
                                 no  │
                              attempt < max?
                                 yes │
                                     ▼
                              fix_dockerfile ──→ build   (retry)
                                 no  │
                                     ▼
                                  error  (emit full attempt history)

Design decisions
────────────────
- ``_run_pipeline`` is a pure function (takes emit callback + settings).
  ``run_pipeline_sync`` wraps it with job-store management.  This split
  makes the logic independently testable without a job store.
- The emit callback is the only side effect.  All state flows through it.
  Phase 6 replaces the list-appending emit with an SSE emitter, with no
  changes to the orchestrator.
- ``fix_dockerfile`` always receives the most recent attempt's error tail,
  not a concatenation of all errors.  The model only needs the last error
  to reason about a targeted fix; older errors add noise.
- ``asyncio.to_thread`` keeps the blocking subprocess calls off the
  FastAPI event loop.
- Run+verify is best-effort: a RunError does not fail the job — we still
  emit "done" with run_success=False so the user gets the Dockerfile even
  when the container health check cannot be confirmed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from app.agent.analyzer import analyze_repo
from app.agent.builder import BuildTimeoutError, DockerNotAvailableError, build_image
from app.agent.cloner import CloneError, InvalidRepoURLError, cloned_repo
from app.agent.generator import GeneratorError, fix_dockerfile, generate_dockerfile
from app.agent.runner import RunError, run_and_verify
from app.config import get_settings
from app.models.job import ForgeJob, clear_all_jobs, create_job, get_job

# ── Typed event helpers (keeps orchestrator code readable) ───────────────────

_Emit = Callable[[dict[str, Any]], None]


def _step(emit: _Emit, step: str) -> None:
    emit({"type": "step_started", "step": step})


def _log(emit: _Emit, line: str) -> None:
    emit({"type": "log_line", "line": line})


# ── Core pipeline logic ───────────────────────────────────────────────────────


def _run_pipeline(
    repo_url: str,
    emit: _Emit,
    *,
    max_attempts: int,
    clone_timeout_s: int,
    max_repo_mb: int,
    build_timeout_s: int,
    run_timeout_s: int,
    container_memory: str,
    container_cpus: str,
    job_id: str,
) -> dict[str, Any] | None:
    """
    Run the full clone → analyze → generate → build → run+verify loop.

    Every side-effect goes through *emit*.  Returns the serialised
    :class:`~app.models.generator_output.GeneratorOutput` on success,
    ``None`` on failure (after emitting an error event).

    Separated from job-store management so tests can inject a plain list
    collector as *emit* without needing the job store.
    """
    image_tag = f"dockerforge-{job_id[:8]}"

    try:
        _step(emit, "clone")
        with cloned_repo(repo_url, timeout_s=clone_timeout_s, max_mb=max_repo_mb) as repo_path:

            _step(emit, "analyze")
            profile = analyze_repo(repo_path, url=repo_url)

            attempt_history: list[dict[str, Any]] = []

            for attempt in range(1, max_attempts + 1):
                emit({"type": "attempt", "number": attempt, "max": max_attempts})

                # ── Generate / fix ────────────────────────────────────────────
                _step(emit, "generate")
                if attempt == 1:
                    output = generate_dockerfile(profile)
                else:
                    prev = attempt_history[-1]
                    output = fix_dockerfile(
                        profile,
                        previous_dockerfile=prev["dockerfile"],
                        build_error_tail=prev["error_tail"],
                        attempt_number=attempt,
                    )

                # ── Build ─────────────────────────────────────────────────────
                _step(emit, "build")
                result = build_image(
                    repo_path,
                    image_tag,
                    dockerfile_content=output.dockerfile,
                    dockerignore_content=output.dockerignore,
                    timeout_s=build_timeout_s,
                    emit=lambda line: _log(emit, line),
                )

                if result.success:
                    emit({
                        "type": "build_result",
                        "success": True,
                        "attempt": attempt,
                        "image_tag": image_tag,
                    })

                    # ── Run + verify ──────────────────────────────────────────
                    _step(emit, "run")
                    run_success = False
                    run_message = ""
                    run_error = ""
                    try:
                        run_result = run_and_verify(
                            image_tag,
                            healthcheck_type=output.healthcheck.type,
                            healthcheck_detail=output.healthcheck.detail,
                            container_port=output.exposed_port,
                            container_name=f"dockerforge-{job_id[:12]}",
                            timeout_s=run_timeout_s,
                            memory_limit=container_memory,
                            cpus_limit=container_cpus,
                            emit_log=lambda line: _log(emit, line),
                        )
                        run_success = run_result.success
                        run_message = run_result.message
                    except RunError as exc:
                        run_error = str(exc)
                        emit({"type": "log_line", "line": f"[runner] {exc}"})

                    emit({
                        "type": "run_result",
                        "success": run_success,
                        "message": run_message,
                        "error": run_error,
                    })

                    output_dict = output.model_dump()
                    emit({
                        "type": "done",
                        "dockerfile": output.dockerfile,
                        "dockerignore": output.dockerignore,
                        "reasoning": output.reasoning,
                        "attempts": attempt,
                        "image_tag": image_tag,
                        "run_success": run_success,
                        "output": output_dict,
                    })
                    return output_dict

                # ── Build failed — record attempt, maybe retry ─────────────
                attempt_history.append({
                    "attempt": attempt,
                    "dockerfile": output.dockerfile,
                    "error_tail": result.error_tail,
                    "reasoning": output.reasoning,
                })
                emit({
                    "type": "build_result",
                    "success": False,
                    "attempt": attempt,
                    "error_tail": result.error_tail,
                })

            # All attempts exhausted without success
            emit({
                "type": "error",
                "message": (
                    f"Build failed after {max_attempts} attempt(s). "
                    "See attempt_history for details."
                ),
                "attempt_history": attempt_history,
            })
            return None

    except InvalidRepoURLError as exc:
        emit({"type": "error", "message": f"Invalid repo URL: {exc}"})
        return None
    except CloneError as exc:
        emit({"type": "error", "message": f"Clone failed: {exc}"})
        return None
    except GeneratorError as exc:
        emit({"type": "error", "message": f"LLM generation failed: {exc}"})
        return None
    except (BuildTimeoutError, DockerNotAvailableError) as exc:
        emit({"type": "error", "message": str(exc)})
        return None
    except Exception as exc:
        emit({"type": "error", "message": f"Unexpected error: {exc}"})
        return None


# ── Job-store wrappers ────────────────────────────────────────────────────────


def run_pipeline_sync(job_id: str) -> None:
    """
    Synchronous pipeline runner — call via ``asyncio.to_thread`` from async
    code (e.g. a FastAPI background task) to avoid blocking the event loop.
    """
    job = get_job(job_id)
    if job is None:
        return

    job.status = "running"
    settings = get_settings()

    def emit(event: dict[str, Any]) -> None:
        job.append_event(event)

    output_dict = _run_pipeline(
        job.repo_url,
        emit,
        max_attempts=settings.max_build_attempts,
        clone_timeout_s=settings.clone_timeout_seconds,
        max_repo_mb=settings.max_repo_mb,
        build_timeout_s=settings.build_timeout_seconds,
        run_timeout_s=settings.run_timeout_seconds,
        container_memory=settings.container_memory,
        container_cpus=settings.container_cpus,
        job_id=job_id,
    )

    job.status = "done" if output_dict is not None else "failed"
    job.output = output_dict
    job.finished_at = datetime.now(timezone.utc)


async def run_pipeline_background(job_id: str) -> None:
    """
    Async wrapper: runs ``run_pipeline_sync`` in the default thread pool
    so the FastAPI event loop is never blocked by subprocess calls.
    """
    await asyncio.to_thread(run_pipeline_sync, job_id)
