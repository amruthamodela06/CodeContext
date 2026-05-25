import os

# Ensure required env is set BEFORE app modules import config.
# Tests that need a real database override this fixture with a live Postgres URL.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://codecontext:codecontext@localhost:5433/codecontext",
)

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client() -> AsyncClient:
    """HTTP client wired directly to the FastAPI app (no network)."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
