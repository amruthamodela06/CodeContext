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
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    frontend_origin: str = "http://localhost:3000"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields populated from env, not kwargs
