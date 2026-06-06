"""
Container runner and health-check verifier.

Design decisions
────────────────
- Four verification modes driven by ``GeneratorOutput.healthcheck``:
    http   — httpx polling loop until the server returns 2xx
    tcp    — socket.create_connection polling loop until the port accepts
    log    — ``docker logs`` polling until a substring appears in output
    exit   — ``docker run --rm`` (foreground); verifies returncode == 0

- ``exit`` mode is for batch/tool containers that produce output and exit
  (e.g. compilers, CLI tools).  Every other mode starts the container in
  detached mode (-d), polls the health check, then stops+removes it in a
  ``finally`` block so cleanup is guaranteed even on exceptions.

- Resource limits (--memory / --cpus) are applied via docker run flags.
  This keeps untrusted images from consuming all host resources.  Values
  come from Settings and are passed in by the orchestrator.

- All docker CLI interactions use subprocess.  No Python Docker SDK is
  added — it would pull in a heavy dependency and its async model does not
  fit here.

- The emit_log callback mirrors the contract in builder.py: one string
  per line so the orchestrator can forward it to the SSE stream.
"""

from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class RunResult:
    success: bool
    container_id: str   # empty string for exit-mode runs
    logs: str
    message: str
    error: str = ""


class RunError(RuntimeError):
    """Container failed to start or health-check timed out."""


_POLL_INTERVAL = 2   # seconds between health-check retries
_LOG_TAIL = 50       # lines captured for RunResult.logs


def run_and_verify(
    image_tag: str,
    *,
    healthcheck_type: str,
    healthcheck_detail: str,
    container_port: int | None,
    container_name: str,
    timeout_s: int = 60,
    memory_limit: str = "256m",
    cpus_limit: str = "0.5",
    emit_log: Callable[[str], None] = lambda _: None,
) -> RunResult:
    """
    Start *image_tag* in a container, verify it via *healthcheck_type*,
    then stop and remove the container.

    :param healthcheck_type: ``"http"``, ``"tcp"``, ``"log"``, or ``"exit"``
    :param healthcheck_detail:
        - ``http``: URL path, e.g. ``"/"`` or ``"/health"``
        - ``tcp``: port as string, e.g. ``"8000"``
        - ``log``: substring to wait for in docker logs, e.g. ``"Listening on"``
        - ``exit``: expected returncode as string (default ``"0"``)
    :param container_port: host-port mapping for http/tcp checks; may be ``None``
    :param container_name: ``--name`` for the container (helps with cleanup)
    :param timeout_s: total seconds before giving up on the health check
    :param memory_limit: ``--memory`` flag value, e.g. ``"256m"``
    :param cpus_limit: ``--cpus`` flag value, e.g. ``"0.5"``
    :param emit_log: callback for streaming individual lines

    :raises RunError: container failed to start or health-check timed out
    """
    emit_log(f"[runner] starting container from image '{image_tag}'")

    if healthcheck_type == "exit":
        return _run_exit_mode(
            image_tag,
            healthcheck_detail=healthcheck_detail,
            memory_limit=memory_limit,
            cpus_limit=cpus_limit,
            timeout_s=timeout_s,
            emit_log=emit_log,
        )

    # Detached modes: http, tcp, log
    container_port = container_port or _parse_port(healthcheck_detail)
    container_id, host_port = _start_detached(
        image_tag,
        container_name=container_name,
        container_port=container_port,
        memory_limit=memory_limit,
        cpus_limit=cpus_limit,
        emit_log=emit_log,
    )

    try:
        if healthcheck_type == "http":
            _verify_http(host_port, healthcheck_detail, timeout_s=timeout_s, emit_log=emit_log)
        elif healthcheck_type == "tcp":
            _verify_tcp(host_port, timeout_s=timeout_s, emit_log=emit_log)
        elif healthcheck_type == "log":
            _verify_log(container_id, healthcheck_detail, timeout_s=timeout_s, emit_log=emit_log)
        else:
            raise RunError(f"Unknown healthcheck type: '{healthcheck_type}'")

        logs = _docker_logs(container_id)
        emit_log("[runner] health check passed")
        return RunResult(
            success=True,
            container_id=container_id,
            logs=logs,
            message=f"{healthcheck_type.upper()} health check passed",
        )

    except RunError:
        raise
    finally:
        _stop_and_remove(container_id, emit_log=emit_log)


# ── Exit mode ─────────────────────────────────────────────────────────────────


def _run_exit_mode(
    image_tag: str,
    *,
    healthcheck_detail: str,
    memory_limit: str,
    cpus_limit: str,
    timeout_s: int,
    emit_log: Callable[[str], None],
) -> RunResult:
    expected_code = int(healthcheck_detail) if healthcheck_detail.isdigit() else 0
    cmd = [
        "docker", "run", "--rm",
        "--memory", memory_limit,
        "--cpus", cpus_limit,
        image_tag,
    ]
    emit_log(f"[runner] docker run --rm {image_tag}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunError(f"Container run timed out after {timeout_s}s") from exc
    except FileNotFoundError as exc:
        raise RunError("'docker' binary not found — is Docker installed?") from exc

    output = (proc.stdout + proc.stderr).strip()
    for line in output.splitlines():
        emit_log(line)

    if proc.returncode != expected_code:
        raise RunError(
            f"Container exited with code {proc.returncode} "
            f"(expected {expected_code}):\n{output[-500:]}"
        )

    emit_log(f"[runner] container exited {proc.returncode} — OK")
    return RunResult(
        success=True,
        container_id="",
        logs=output,
        message=f"Container exited {proc.returncode} as expected",
    )


# ── Detached helpers ──────────────────────────────────────────────────────────


def _start_detached(
    image_tag: str,
    *,
    container_name: str,
    container_port: int | None,
    memory_limit: str,
    cpus_limit: str,
    emit_log: Callable[[str], None],
) -> tuple[str, int | None]:
    """
    Run *image_tag* in detached mode.

    Returns ``(container_id, host_port)`` where *host_port* is the OS-assigned
    port on the host side of the mapping (or ``None`` if no port mapping was
    requested).

    We always use ``-p 0:{container_port}`` so the OS picks a free host port,
    avoiding "port already allocated" errors when the natural port (e.g. 3000
    or 8000) is already in use by another process on the host.
    """
    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--memory", memory_limit,
        "--cpus", cpus_limit,
    ]
    if container_port:
        cmd += ["-p", f"0:{container_port}"]   # 0 → let the OS pick a free host port
    cmd.append(image_tag)

    emit_log(f"[runner] docker run -d {image_tag}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise RunError("'docker' binary not found — is Docker installed?") from exc

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise RunError(f"Failed to start container: {err}")

    container_id = result.stdout.strip()

    host_port: int | None = None
    if container_port:
        host_port = _query_host_port(container_id, container_port, emit_log=emit_log)

    emit_log(f"[runner] container started: {container_id[:12]}, host port: {host_port}")
    return container_id, host_port


def _query_host_port(
    container_id: str,
    container_port: int,
    *,
    emit_log: Callable[[str], None],
) -> int:
    """
    Return the host port Docker assigned for *container_port*.

    ``docker port`` output looks like::

        0.0.0.0:49152
        :::49152

    We take the last token after the final ``:`` on the first line.
    """
    result = subprocess.run(
        ["docker", "port", container_id, str(container_port)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RunError(
            f"Could not query host port for container port {container_port}: "
            f"{result.stderr.strip()}"
        )
    raw = result.stdout.strip().splitlines()[0]
    host_port = int(raw.rsplit(":", 1)[-1])
    emit_log(f"[runner] container port {container_port} → host port {host_port}")
    return host_port


def _stop_and_remove(container_id: str, *, emit_log: Callable[[str], None]) -> None:
    if not container_id:
        return
    emit_log(f"[runner] stopping container {container_id[:12]}")
    subprocess.run(
        ["docker", "stop", container_id],
        capture_output=True, timeout=15,
    )
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True, timeout=10,
    )


def _docker_logs(container_id: str) -> str:
    result = subprocess.run(
        ["docker", "logs", "--tail", str(_LOG_TAIL), container_id],
        capture_output=True, text=True, timeout=10,
    )
    return (result.stdout + result.stderr).strip()


# ── Verification modes ────────────────────────────────────────────────────────


def _verify_http(
    port: int,
    path: str,
    *,
    timeout_s: int,
    emit_log: Callable[[str], None],
) -> None:
    import httpx

    url = f"http://localhost:{port}{path if path.startswith('/') else '/' + path}"
    deadline = time.monotonic() + timeout_s
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code < 300:
                emit_log(f"[runner] HTTP {resp.status_code} on {url} (attempt {attempt})")
                return
            emit_log(f"[runner] HTTP {resp.status_code} — retrying…")
        except Exception as exc:
            emit_log(f"[runner] HTTP probe failed: {exc} — retrying…")
        time.sleep(_POLL_INTERVAL)

    raise RunError(f"HTTP health check timed out after {timeout_s}s: {url}")


def _verify_tcp(
    port: int,
    *,
    timeout_s: int,
    emit_log: Callable[[str], None],
) -> None:
    deadline = time.monotonic() + timeout_s
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            with socket.create_connection(("localhost", port), timeout=3):
                emit_log(f"[runner] TCP {port} open (attempt {attempt})")
                return
        except OSError as exc:
            emit_log(f"[runner] TCP {port} not yet open: {exc} — retrying…")
        time.sleep(_POLL_INTERVAL)

    raise RunError(f"TCP health check timed out after {timeout_s}s on port {port}")


def _verify_log(
    container_id: str,
    substring: str,
    *,
    timeout_s: int,
    emit_log: Callable[[str], None],
) -> None:
    deadline = time.monotonic() + timeout_s
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        logs = _docker_logs(container_id)
        if substring in logs:
            emit_log(f"[runner] found '{substring}' in logs (attempt {attempt})")
            return
        emit_log(f"[runner] waiting for '{substring}' in logs… (attempt {attempt})")
        time.sleep(_POLL_INTERVAL)

    raise RunError(
        f"Log health check timed out after {timeout_s}s: "
        f"'{substring}' never appeared in container logs"
    )


# ── Utility ───────────────────────────────────────────────────────────────────


def _parse_port(detail: str) -> int | None:
    """Best-effort port extraction from a healthcheck detail string."""
    try:
        return int(detail.strip())
    except ValueError:
        return None
