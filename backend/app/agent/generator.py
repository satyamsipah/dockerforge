"""
Dockerfile generator — turns a RepoProfile into a validated GeneratorOutput.

Two LLM backends are supported, selected by the GEMINI_BASE_URL env var:

  GEMINI_BASE_URL empty (default)
    → google-genai SDK with response_schema=GeneratorOutput (native JSON mode).
      The SDK enforces the Pydantic schema server-side before we see the response.

  GEMINI_BASE_URL set (e.g. https://openrouter.ai/api/v1)
    → openai-compatible client (works with OpenRouter, LiteLLM, any OAI proxy).
      JSON mode is requested via response_format and the schema is embedded in
      the system prompt.  The response is parsed manually with model_validate_json.

In both cases the public API is identical: generate_dockerfile / fix_dockerfile
return a GeneratorOutput or raise GeneratorError.
"""

from __future__ import annotations

import json
import re

# google-genai is always installed (native Gemini path).
# openai is lazily imported inside _call_openai_compat so it's optional.
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

    :raises GeneratorError: API key missing, call failed, or unparseable response.
    """
    return _call_llm(_build_generation_prompt(profile))


def fix_dockerfile(
    profile: RepoProfile,
    previous_dockerfile: str,
    build_error_tail: str,
    attempt_number: int,
) -> GeneratorOutput:
    """
    Ask the model for a targeted fix given a previous failing Dockerfile.

    :raises GeneratorError: same as :func:`generate_dockerfile`.
    """
    return _call_llm(
        _build_fix_prompt(profile, previous_dockerfile, build_error_tail, attempt_number)
    )


# ── Router ────────────────────────────────────────────────────────────────────


def _call_llm(prompt: str) -> GeneratorOutput:
    """Dispatch to the right backend based on GEMINI_BASE_URL."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeneratorError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    if settings.gemini_base_url:
        return _call_openai_compat(prompt, settings)
    return _call_gemini_native(prompt, settings)


# ── Backend A: native google-genai (direct Gemini, no base-url) ───────────────


def _call_gemini_native(prompt: str, settings) -> GeneratorOutput:  # type: ignore[type-arg]
    """
    Use the google-genai SDK with response_schema for strict structured output.
    The SDK enforces GeneratorOutput's shape before we see the response.
    """
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

    # Prefer the SDK's native Pydantic parse; fall back to manual JSON parsing.
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


# ── Backend B: OpenAI-compatible client (OpenRouter, LiteLLM, …) ─────────────


def _call_openai_compat(prompt: str, settings) -> GeneratorOutput:  # type: ignore[type-arg]
    """
    Use the openai package pointed at GEMINI_BASE_URL.

    Structured output is requested two ways:
      1. response_format={"type": "json_object"} — most OAI-proxy models honour this.
      2. The JSON schema is embedded in the system prompt as a belt-and-suspenders
         measure for models that ignore response_format.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise GeneratorError(
            "The 'openai' package is required when GEMINI_BASE_URL is set. "
            "Run: pip install openai"
        ) from exc

    client = OpenAI(
        api_key=settings.gemini_api_key,
        base_url=settings.gemini_base_url,
    )

    schema = GeneratorOutput.model_json_schema()
    system_msg = (
        "You are a Docker expert. "
        "Respond ONLY with a valid JSON object — no markdown fences, no prose.\n"
        "The JSON must match this schema exactly:\n"
        f"{json.dumps(schema, indent=2)}"
    )

    try:
        response = client.chat.completions.create(
            model=settings.gemini_model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        raise GeneratorError(f"OpenAI-compat API call failed: {exc}") from exc

    content = response.choices[0].message.content or ""
    content = _strip_code_fences(content).strip()

    try:
        return GeneratorOutput.model_validate_json(content)
    except Exception as exc:
        preview = content[:400]
        raise GeneratorError(
            f"Could not parse LLM response into GeneratorOutput.\n"
            f"Response preview (first 400 chars):\n{preview}"
        ) from exc


def _strip_code_fences(text: str) -> str:
    """Remove ```json … ``` wrapping that some models add despite JSON-only instructions."""
    return re.sub(r"^```[a-z]*\n?", "", re.sub(r"\n?```$", "", text.strip()))


# ── Prompt builders ───────────────────────────────────────────────────────────


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
    """Prompt for a targeted fix of a failing Dockerfile."""
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
