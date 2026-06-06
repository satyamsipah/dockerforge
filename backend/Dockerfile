FROM python:3.11-slim

# ── System deps ───────────────────────────────────────────────────────────────
# git   — shallow-clones public repos (the agent's first step)
# docker.io — Docker CLI; the daemon is never started inside this container.
#             All `docker build` / `docker run` calls go to the host daemon
#             via the socket mounted at /var/run/docker.sock (see compose file).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY app/ app/

# ── Non-root user + Docker socket access ─────────────────────────────────────
# The Docker socket is owned by the `docker` group on the host (GID varies by
# distro — typically 999 on Ubuntu/Debian).  Pass DOCKER_GID at build time if
# your host uses a different GID:
#   docker compose build --build-arg DOCKER_GID=$(getent group docker | cut -d: -f3)
ARG DOCKER_GID=999

# Create the docker group with the right GID if it doesn't exist, then create
# a non-root app user and add them to that group.
RUN (groupmod --gid ${DOCKER_GID} docker 2>/dev/null || \
     groupadd --gid ${DOCKER_GID} dockerhost) \
    && useradd --uid 1001 --gid 0 --shell /bin/bash --create-home appuser \
    && usermod -aG ${DOCKER_GID} appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
