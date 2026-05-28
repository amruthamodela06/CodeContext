import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# Point app config at a dedicated test DB *before* any app.* import that reads
# DATABASE_URL at module-load time (app.db.engine in particular). We force-
# overwrite — tests must NEVER touch the dev DB even if the developer has
# DATABASE_URL set in their shell.
_TEST_DB_URL = "postgresql+asyncpg://codecontext:codecontext@localhost:5433/codecontext_test"
os.environ["DATABASE_URL"] = _TEST_DB_URL
# Tests use the deterministic FakeEmbedder (384-dim, no model load). The slow
# real-model test instantiates SentenceTransformersEmbedder directly and is
# gated behind RUN_SLOW, so it doesn't depend on this.
os.environ["EMBEDDING_PROVIDER"] = "mock"

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

import app.db as app_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

# pytest-asyncio uses a per-test event loop in auto mode. asyncpg connections
# are bound to the loop they were created on, so the default QueuePool keeps
# stale loop-bound connections that fail on the next test. Swap the app's
# engine for one using NullPool — each request creates and closes its own
# connection. Adds ~10 ms per request; negligible for the test suite.
app_db.engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
app_db.SessionLocal = async_sessionmaker(app_db.engine, expire_on_commit=False)


# --- Test DB lifecycle ----------------------------------------------------


async def _drop_and_create_test_db() -> None:
    conn = await asyncpg.connect(
        host="localhost",
        port=5433,
        user="codecontext",
        password="codecontext",
        database="postgres",
    )
    try:
        await conn.execute("DROP DATABASE IF EXISTS codecontext_test")
        await conn.execute("CREATE DATABASE codecontext_test")
    finally:
        await conn.close()


async def _drop_test_db() -> None:
    conn = await asyncpg.connect(
        host="localhost",
        port=5433,
        user="codecontext",
        password="codecontext",
        database="postgres",
    )
    try:
        await conn.execute("DROP DATABASE IF EXISTS codecontext_test")
    finally:
        await conn.close()


async def _create_tables() -> None:
    engine = create_async_engine(_TEST_DB_URL)
    try:
        async with engine.begin() as conn:
            # pgvector extension must exist before create_all builds a table
            # with a vector column. Tests skip Alembic, so we run it here too.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def setup_test_database() -> Iterator[None]:
    """Create + drop the test DB. Not autouse — pulled in transitively by db-using fixtures.

    This lets non-DB tests (test_url, test_filter, test_language, test_healthz)
    avoid the ~1-2 s of DB setup entirely.
    """
    asyncio.run(_drop_and_create_test_db())
    asyncio.run(_create_tables())
    yield
    asyncio.run(_drop_test_db())


# --- Per-test fixtures ----------------------------------------------------


@pytest_asyncio.fixture
async def session(setup_test_database: None) -> AsyncIterator[AsyncSession]:
    """A SQLAlchemy session bound to the test DB."""
    engine = create_async_engine(_TEST_DB_URL)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as s:
            yield s
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def _clean_db(setup_test_database: None) -> AsyncIterator[None]:
    """Truncate repo + file (via CASCADE) so each test starts from a known state.

    Apply per-test via `pytestmark = pytest.mark.usefixtures("_clean_db")` at
    the top of test files that hit the DB (see test_ingest.py).
    """
    engine = create_async_engine(_TEST_DB_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("TRUNCATE repo CASCADE"))
            await conn.commit()
    finally:
        await engine.dispose()
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """HTTP client wired directly to the FastAPI app (no network).

    The app's DB session dependency uses the test DB via the env override above.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Materialize the vendored fixture as a real git repo in tmp_path.

    Copies backend/tests/fixtures/sample-repo/ to tmp_path/sample-repo/ and
    renames dot-git/ -> .git/ so the directory is clone-able and walkable.
    """
    src = Path(__file__).parent / "fixtures" / "sample-repo"
    dst = tmp_path / "sample-repo"
    shutil.copytree(src, dst)
    (dst / "dot-git").rename(dst / ".git")
    return dst
