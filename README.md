# 🐳 DockerForge

> An AI agent that takes a public GitHub repo URL, analyses the codebase, and
> produces a **Dockerfile that actually builds and runs** — verifying its own
> output and self-correcting on build failure.

DockerForge doesn't just ask an LLM to "write a Dockerfile." It runs a closed
agent loop: **clone → analyse → generate → build → (reason & retry ×3) → run →
verify**, streaming every step to the UI in real time.

> **Status:** 🚧 Under active construction — built in phases.
> **Phase 1 (skeleton) complete:** runnable FastAPI backend + Vite/React/Tailwind
> frontend + project scaffold. Later phases add the clone/analyse/generate/
> build/run agent loop.

---

## Why this is interesting

The hard part isn't generating a Dockerfile — it's **verifying** it. DockerForge
builds the image it generates, reads the build error if it fails, reasons about a
targeted fix, and retries (up to 3 times) before running the container and
checking that it actually responds. That verify → reason → retry loop is the core
of the project.

---

## Architecture

```
[ React UI ] --POST /api/forge {repo_url}--> [ FastAPI ]
     ^                                            |
     |------ SSE: live agent steps + build logs --|
                                                  v
                                        [ Agent Orchestrator ]
   clone -> analyze -> generate -> build -> (retry?) -> run -> verify -> cleanup
                                                  |
                                          [ Gemini (structured JSON output) ]
                                                  |
                                          [ Docker engine via host socket ]
```

A full Mermaid diagram and the agent-loop walkthrough land in the final phase.

### Repository layout

```
dockerforge/
├── backend/                # Python 3.11 + FastAPI
│   ├── app/
│   │   ├── main.py         # FastAPI app, CORS, router wiring
│   │   ├── config.py       # Typed settings loaded from .env
│   │   ├── api/            # HTTP + SSE routes (/api/health, /api/forge …)
│   │   ├── agent/          # cloner, analyzer, generator, builder, runner, orchestrator
│   │   └── models/         # Pydantic schemas (RepoProfile, events …)
│   ├── tests/
│   └── requirements.txt
├── frontend/               # React (Vite) + Tailwind — live timeline + log panel
├── .env.example
└── README.md
```

---

## Setup (local dev)

> Prerequisites: Python 3.11+ (3.13 works for dev), Node 18+, and a running
> Docker daemon (required from Phase 4 onward).

```bash
# 1. Configure secrets
cp .env.example .env        # then add your GEMINI_API_KEY

# 2. Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# health check: curl http://localhost:8000/api/health

# 3. Frontend (in a second terminal)
cd frontend
npm install
npm run dev                 # http://localhost:5173
```

Running via Docker (self-Dockerization, socket mount) is added in Phase 7.

---

## LLM provider — and why Gemini

DockerForge uses **Google Gemini** (`gemini-2.0-flash`) as the agent's reasoning
engine. Rationale (expanded in the final README): generous free tier, fast
latency for an interactive loop, strong code reasoning, and native structured /
JSON output so the generated Dockerfile is parsed reliably instead of scraped
from prose. _(Note: Claude Code was used as the pair-programmer to build this
project; Gemini is the model that runs inside the shipped product — they're
separate concerns.)_

---

## Known limitations / edge cases

_To be completed in the final phase. Will cover: requires a running Docker daemon
(so it can't run on serverless free tiers), arm64/amd64 image-arch caveats on
Apple Silicon, the 3-retry ceiling, and the heuristic nature of the "does it
respond?" check for unusual app types._

---

## Security posture

Cloned repositories are **untrusted code**. DockerForge isolates each clone in a
temp dir, applies CPU/memory limits and timeouts to every build and run, never
mounts host paths into the built container, and guards the input URL against
SSRF. The full write-up (including the host-Docker-socket tradeoff) is in the
final phase.

---

_Built by Satyam Maddheshiya. This README states only what the code actually does._
