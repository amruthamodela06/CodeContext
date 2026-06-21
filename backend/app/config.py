from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables.

    Reads `.env` from the repo root (where you run `make backend-dev` from).
    """

    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pw@host:5432/db",
    )
    github_token: str = Field(
        default="", description="Optional GitHub PAT for higher API rate limits."
    )
    # ADR 0009. Carries the full embedder identity: bge-small (default),
    # bge-base, mock (tests), or a future provider id.
    embedding_provider: str = Field(default="bge-small")

    # --- LLM (Slice 4, ADR 0007 + 0010) ---
    # Provider id: gemini (default, free tier), ollama (offline), mock (tests),
    # openai/anthropic (stubbed, ablation-only).
    llm_provider: str = Field(default="gemini")
    gemini_api_key: str = Field(
        default="", description="Google AI Studio key for Gemini's OpenAI-compatible endpoint."
    )
    gemini_model: str = Field(default="gemini-2.0-flash")
    # Free-tier guardrail — cap requests/minute so a dev hot-loop can't burn quota.
    gemini_rpm_limit: int = Field(default=15)
    ollama_base_url: str = Field(default="http://localhost:11434/v1")
    ollama_model: str = Field(default="qwen2.5-coder:3b-instruct")

    # --- History ingestion (Slice 5b, ADR 0011) ---
    # How far back to fetch commits / PRs / issues. 12 months matches PRD
    # §6.5; raise for a backfill ingest, lower for a fast demo.
    history_ingestion_months: int = Field(default=12, ge=1, le=120)

    # --- Query classifier (Slice 5e, ADR 0012) ---
    # 'keyword' (default) -- sub-ms, no LLM call. 'llm' -- one extra
    # LLM call per query; better on ambiguous phrasings, costs latency.
    query_classifier: str = Field(default="keyword")

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    frontend_origin: str = "http://localhost:3000"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields populated from env, not kwargs
