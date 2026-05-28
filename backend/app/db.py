from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_settings = get_settings()

# Note: we do NOT register the pgvector asyncpg binary codec. SQLAlchemy's
# pgvector `Vector` type serializes list[float] to pgvector's text format and
# Postgres parses it on both insert and `<=>` queries. Registering the asyncpg
# codec on top of that causes a double-encode conflict (the codec receives an
# already-stringified value). The codec is only for raw asyncpg usage. (ADR 0009)
engine = create_async_engine(_settings.database_url, future=True, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session bound to the request lifecycle."""
    async with SessionLocal() as session:
        yield session
