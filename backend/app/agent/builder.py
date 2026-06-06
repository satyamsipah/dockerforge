"""
Docker image builder with line-by-line streaming.

Design decisions
────────────────
- subprocess over Docker Python SDK: ``subprocess.Popen`` lets us stream
  stdout/stderr line-by-line without JSON-unwrapping each chunk (the SDK
  returns ``{"stream": "...\\n"}`` objects).  It also keeps the dependency
  footprint minimal.
- Merged stderr into stdout (``stderr=STDOUT``): docker build writes all
  useful output to stdout, but some internal messages go to stderr.  Merging
  means ``emit()`` sees a single ordered stream.
- Thread-based read + ``proc.wait(timeout=)`` for timeout enforcement.
  Iterating ``proc.stdout`` in the main thread blocks until the process
  produces output — that is fine for line-rate build logs.  A separate call
  to ``proc.wait(timeout=...)`` enforces the hard deadline; when it raises
  ``TimeoutExpired`` we kill the process and the reader thread drains
  naturally.
- ``--no-cache``: ensures every build starts clean so the generated Dockerfile
  is tested in isolation.  Without it, a stale layer could mask a real bug
  in the Dockerfile the model produced.
- Error tail: on failure we keep the last 30 lines.  The retry prompt
  (Phase 3) sends only this tail to the model — it is almost always enough
  context to diagnose a build error, and keeps token usage bounded.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class BuildResult:
    success: bool
    image_tag: str
    exit_code: int
    # On failure: the last ``_ERROR_TAIL_LINES`` lines of build output.
    error_tail: str = ""
    # All output lines (for emitting to the SSE stream and storing in the job).
    log_lines: list[str] = field(default_factory=list)


class BuildTimeoutError(RuntimeError):
    """``docker build`` exceeded the configured time limit."""


class DockerNotAvailableError(RuntimeError):
    """Docker daemon is unreachable — the socket isn't mounted or the daemon is down."""


_ERROR_TAIL_LINES = 30


def build_image(
    context_path: Path,
    image_tag: str,
    dockerfile_content: str,
    *,
    dockerignore_content: str | None = None,
    timeout_s: int = 600,
    emit: Callable[[str], None] = lambda _: None,
) -> BuildResult:
    """
    Write *dockerfile_content* to *context_path*/Dockerfile, run
    ``docker build``, and return a :class:`BuildResult`.

    Every output line is passed to *emit* as it arrives (for SSE streaming).

    :raises BuildTimeoutError: build exceeded *timeout_s* seconds.
    :raises DockerNotAvailableError: Docker daemon is not reachable.
    :raises OSError: *context_path* is not accessible.
    """
    dockerfile_path = context_path / "Dockerfile"
    dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

    if dockerignore_content:
        (context_path / ".dockerignore").write_text(dockerignore_content, encoding="utf-8")

    cmd = [
        "docker", "build",
        "--no-cache",
        "-t", image_tag,
        ".",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(context_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge so emit sees one ordered stream
            text=True,
            bufsize=1,                  # line-buffered
        )
    except FileNotFoundError as exc:
        raise DockerNotAvailableError(
            "Could not find the 'docker' binary.  "
            "Is Docker installed and on PATH?"
        ) from exc

    all_lines: list[str] = []
    timed_out = threading.Event()

    def _read_stdout() -> None:
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
            all_lines.append(line)
            emit(line)

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()

    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out.set()
        proc.kill()
        proc.wait()

    reader.join(timeout=5)  # let the reader flush remaining lines

    if timed_out.is_set():
        raise BuildTimeoutError(
            f"docker build timed out after {timeout_s}s for image '{image_tag}'"
        )

    # Check for "Cannot connect to the Docker daemon" in output
    joined = "\n".join(all_lines)
    if proc.returncode != 0 and "Cannot connect to the Docker daemon" in joined:
        raise DockerNotAvailableError(
            "Docker daemon is not running or the socket is not mounted.  "
            "Start Docker Desktop or run 'sudo dockerd'."
        )

    success = proc.returncode == 0
    error_tail = ""
    if not success:
        error_tail = "\n".join(all_lines[-_ERROR_TAIL_LINES:])

    return BuildResult(
        success=success,
        image_tag=image_tag,
        exit_code=proc.returncode,
        error_tail=error_tail,
        log_lines=all_lines,
    )
