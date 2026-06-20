import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, update

from app.api import router
from app.config import get_settings
from app.db import SessionLocal
from app.embeddings import get_embedder
from app.models import EntityEmbedding, Repo

log = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup hygiene (ADR 0009).

    1. Orphan recovery: a uvicorn restart mid-embed leaves repos stuck at
       'in_progress'. Reset them to 'failed' so the UI shows a retryable state.
    2. Dimension guard: if stored embeddings were produced by a model whose
       dimension differs from the active embedder, refuse to start rather than
       corrupt search silently. (`.dimension` is a cheap lookup — no model load.)

    No eager model warm-up: it would reload the ~130 MB model on every
    `--reload` dev restart. The model loads lazily on the first /embed or
    /search instead.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            update(Repo)
            .where(Repo.embedding_status == "in_progress")
            .values(embedding_status="failed")
        )
        if result.rowcount:
            log.warning("reset %d orphaned in_progress embedding job(s) to failed", result.rowcount)

        stored_dim = await session.scalar(select(EntityEmbedding.dimension).limit(1))
        if stored_dim is not None:
            active_dim = get_embedder().dimension
            if stored_dim != active_dim:
                raise RuntimeError(
                    f"entity_embedding rows are {stored_dim}-dim but the active "
                    f"embedder ({get_embedder().name}) is {active_dim}-dim. "
                    "Re-embed all repos or fix EMBEDDING_PROVIDER."
                )
        await session.commit()
    yield


app = FastAPI(title="CodeContext", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
