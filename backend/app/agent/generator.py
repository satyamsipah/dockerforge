"""
Dockerfile generator — one Gemini call that turns a RepoProfile into a
validated GeneratorOutput.

Design decisions
────────────────
- One call per attempt.  The retry loop in the orchestrator (Phase 4) calls
  ``generate_dockerfile`` for the initial attempt and ``fix_dockerfile`` for
  each subsequent one.  Keeping them separate keeps the prompts focused and
  the call graph easy to trace.
- Structured output (JSON mode + response_schema=GeneratorOutput).  Passing
  the Pydantic model as ``response_schema`` lets the Gemini SDK enforce the
  schema before we see the response — no regex, no prose-scraping.  If the
  model produces a structurally invalid response, the SDK raises rather than
  silently returning garbage.
- Prompt sends the RepoProfile, not raw files.  The profile is already the
  distilled, token-efficient representation of the repo (built in Phase 2).
  The key_files field contains the few manifests + entry file that matter.
- No retry inside this module.  A GeneratorError propagates up to the
  orchestrator, which decides whether to retry or surface the failure.
"""

from __future__ import annotations

import json

from google import genai
from google.genai import types

from app.config import get_settings
from app.models.generator_output import GeneratorOutput
from app.models.repo_profile import RepoProfile


class GeneratorError(RuntimeError):
    """Raised when the LLM call fails or returns an unparseable response."""


# ── Public API ────────────────────────────────────────────────────────────────


def generate_dockerfile(profile: RepoProfile) -> GeneratorOutput:
    """
    Generate a Dockerfile for *profile* (first attempt).

    :raises GeneratorError: API key missing, API call failed, or response
        could not be parsed into :class:`GeneratorOutput`.
    """
    return _call_gemini(_build_generation_prompt(profile))


def fix_dockerfile(
    profile: RepoProfile,
    previous_dockerfile: str,
    build_error_tail: str,
    attempt_number: int,
) -> GeneratorOutput:
    """
    Ask the model for a *targeted fix* given a previous failing Dockerfile.

    Called by the orchestrator's retry loop (Phase 4).  The prompt explicitly
    asks for a surgical edit rather than a from-scratch rewrite so that the
    model's fix reasoning is traceable.

    :raises GeneratorError: same as :func:`generate_dockerfile`.
    """
    return _call_gemini(
        _build_fix_prompt(profile, previous_dockerfile, build_error_tail, attempt_number)
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _call_gemini(prompt: str) -> GeneratorOutput:
    """
    Make one Gemini API call with JSON mode and return the parsed output.

    The SDK's ``response_schema`` parameter enforces the GeneratorOutput shape
    before we see the response.  If parsing still fails (e.g. the model
    produces a structurally valid JSON that fails our Pydantic validators), we
    re-raise as :exc:`GeneratorError` with a useful preview.
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeneratorError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    client = genai.Client(api_key=settings.gemini_api_key)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratorOutput,
            ),
        )
    except Exception as exc:
        raise GeneratorError(f"Gemini API call failed: {exc}") from exc

    # Prefer the SDK's native Pydantic parse (set when response_schema is a
    # Pydantic class); fall back to manual JSON parsing.
    if getattr(response, "parsed", None) is not None:
        return response.parsed  # type: ignore[return-value]

    try:
        return GeneratorOutput.model_validate_json(response.text)
    except Exception as exc:
        preview = (response.text or "")[:400]
        raise GeneratorError(
            f"Could not parse Gemini response into GeneratorOutput.\n"
            f"Response preview (first 400 chars):\n{preview}"
        ) from exc


def _build_generation_prompt(profile: RepoProfile) -> str:
    """Prompt for the initial Dockerfile generation from a RepoProfile."""
    meta = {
        "url": profile.url,
        "language": profile.language,
        "framework": profile.framework,
        "entrypoint": profile.entrypoint,
        "start_command": profile.start_command,
        "runtime_version": profile.runtime_version,
        "exposed_port": profile.exposed_port,
        "build_tool": profile.build_tool,
        "has_existing_dockerfile": profile.has_dockerfile,
    }

    key_files_section = _format_key_files(profile)

    return f"""\
You are a Docker expert. Given the structured profile of a GitHub repository,
generate a production-quality Dockerfile that correctly builds and runs the application.

## Repository profile
{json.dumps(meta, indent=2)}

## Key files (manifests and entry point)
{key_files_section}

## Requirements
- Use a **multi-stage build** to minimise the final image size (builder → runtime).
- Run the application as a **non-root user** (create a user with UID 1001).
- Use a **pinned, slim base image** (e.g. python:3.11-slim, node:18-alpine) — never `latest`.
- EXPOSE the correct port; add a HEALTHCHECK instruction in the Dockerfile.
- Generate a `.dockerignore` appropriate for this language (return it in the dockerignore field).
- The `run_command` should be a ready-to-paste `docker run` command that starts
  the container and maps its port, e.g. `docker run --rm -p 5000:5000 app`.
- In the `reasoning` field, briefly explain: base image choice, stage split,
  non-root user approach, and any framework-specific decisions.

Respond with a JSON object matching the required schema exactly.
"""


def _build_fix_prompt(
    profile: RepoProfile,
    previous_dockerfile: str,
    build_error_tail: str,
    attempt_number: int,
) -> str:
    """
    Prompt for a targeted fix of a failing Dockerfile.

    We send the model the previous Dockerfile and the build error tail so it can
    reason about root cause and produce a surgical edit — not a from-scratch
    rewrite.  The attempt number helps it understand how many tries remain.
    """
    meta = {
        "language": profile.language,
        "framework": profile.framework,
        "entrypoint": profile.entrypoint,
        "start_command": profile.start_command,
        "runtime_version": profile.runtime_version,
        "exposed_port": profile.exposed_port,
        "build_tool": profile.build_tool,
    }
    key_files_section = _format_key_files(profile)

    return f"""\
You are a Docker expert. The Dockerfile below failed to build. Diagnose the root
cause from the error output and produce a **targeted fix** — do not rewrite from
scratch unless the entire approach was wrong.

## Repository profile
{json.dumps(meta, indent=2)}

## Key files
{key_files_section}

## Dockerfile that failed (attempt {attempt_number})
```dockerfile
{previous_dockerfile}
```

## Build error (last lines)
```
{build_error_tail}
```

## Instructions
- Identify the single root cause from the build error.
- Apply the minimal change that fixes it.
- Keep all the good parts of the previous Dockerfile (multi-stage, non-root, etc.).
- In `reasoning`, start with one sentence describing the root cause, then explain the fix.

Respond with a JSON object matching the required schema exactly.
"""


def _format_key_files(profile: RepoProfile) -> str:
    if not profile.key_files:
        return "(no key files detected)"
    parts = []
    for kf in profile.key_files:
        parts.append(f"### {kf.path}\n```\n{kf.content}\n```")
    return "\n\n".join(parts)
