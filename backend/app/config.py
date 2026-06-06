"""
Typed application settings.

All runtime configuration lives here and is loaded once from the environment
(or a local `.env` file). Centralising it means every module reads the same
validated values, and the agent's guardrails (timeouts, size caps, retry
ceiling) are tuned in one place instead of being scattered as magic numbers.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Pull from a project-root .env if present; ignore unknown keys so the file
    # can hold frontend-only vars (e.g. VITE_*) without breaking the backend.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "DockerForge"
    debug: bool = False

    # --- LLM provider ---
    # When GEMINI_BASE_URL is empty, the native google-genai SDK is used.
    # When set (e.g. https://openrouter.ai/api/v1), the OpenAI-compatible
    # client is used instead — works with OpenRouter, LiteLLM, and any
    # other proxy that speaks the OpenAI chat-completions API.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = ""  # leave empty for direct Gemini; set for OpenRouter etc.

    # --- CORS: which frontend origins may call the API ---
    cors_origins: str = "http://localhost:5173"

    # --- Agent guardrails ---
    clone_timeout_seconds: int = 60
    max_repo_mb: int = 200
    build_timeout_seconds: int = 600
    run_timeout_seconds: int = 60
    max_build_attempts: int = 3

    # --- Container resource limits (Phase 5) ---
    container_memory: str = "256m"
    container_cpus: str = "0.5"

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a clean list (env var is a comma-separated string)."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
