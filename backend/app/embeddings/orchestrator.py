"""Repo-level embedding orchestrator. See ADR 0009.

Runs as a FastAPI BackgroundTask with its own session (the request session is
closed by the time it runs). Idempotent: deletes the repo's existing vectors
first. Updates Repo.embedding_status / embedding_progress as it goes so the
poll endpoint can report progress. The HNSW index is built AFTER the bulk
insert.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.embeddings import get_embedder
from app.models import CodeChunk, EntityEmbedding, Repo

log = logging.getLogger(__name__)

_BATCH_SIZE = 32
# Single HNSW index across all entity_embedding rows (chunk / commit / pr /
# issue post-Slice-5). The polymorphic widening doesn't change vector ops.
_HNSW_INDEX = "entity_embedding_hnsw_cos"


def _batches(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


async def embed_repo(repo_id: int, session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Embed all of a repo's chunks. Returns the number embedded.

    Failure model differs from chunking: a malformed *file* is common (chunking
    skips it), but embedding failures are catastrophic (model load / OOM). We
    skip a batch whose *encode* fails and keep going; if every batch fails we
    mark the repo `failed` rather than reporting a misleading `done`.
    """
    embedder = get_embedder()

    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:
            log.warning("embed_repo: repo %d not found", repo_id)
            return 0

        repo.embedding_status = "in_progress"
        repo.embedding_progress = 0.0
        await session.commit()

        # Idempotent re-embed: clear this repo's prior CHUNK vectors only.
        # Slice 5's commit/PR/issue embeddings live in the same table but are
        # managed by a separate job — we must not stomp them here.
        await session.execute(
            delete(EntityEmbedding).where(
                EntityEmbedding.repo_id == repo_id,
                EntityEmbedding.entity_type == "chunk",
            )
        )
        await session.commit()

        rows = (
            await session.execute(
                select(CodeChunk.id, CodeChunk.content).where(CodeChunk.repo_id == repo_id)
            )
        ).all()
        total = len(rows)

        if total == 0:
            repo.embedding_status = "done"
            repo.embedding_progress = 1.0
            await session.commit()
            return 0

        embedded = 0
        processed = 0
        for batch in _batches(rows, _BATCH_SIZE):
            ids = [r[0] for r in batch]
            texts = [r[1] for r in batch]
            try:
                # Encode runs in a thread (CPU-bound) and BEFORE any session
                # mutation, so a failure here leaves the session clean — no
                # rollback needed, just skip the batch.
                vectors = await asyncio.to_thread(embedder.embed_batch, texts)
            except Exception:
                log.exception(
                    "embed batch failed (repo %d, %d chunks); skipping",
                    repo_id,
                    len(batch),
                )
            else:
                session.add_all(
                    EntityEmbedding(
                        entity_type="chunk",
                        entity_id=cid,
                        repo_id=repo_id,
                        embedding=vec,
                        model_name=embedder.name,
                        dimension=embedder.dimension,
                    )
                    for cid, vec in zip(ids, vectors, strict=True)
                )
                embedded += len(batch)
            processed += len(batch)
            repo.embedding_progress = processed / total
            await session.commit()

        if embedded == 0:
            repo.embedding_status = "failed"
            repo.embedding_progress = 1.0
            await session.commit()
            return 0

        await _ensure_hnsw_index(session)
        repo.embedding_status = "done"
        repo.embedding_progress = 1.0
        await session.commit()
        return embedded


async def _ensure_hnsw_index(session: AsyncSession) -> None:
    """Create the HNSW cosine index if it doesn't exist (ADR 0009).

    Built after the bulk insert: creating it on an empty table and then
    inserting is ~10x slower at scale. `IF NOT EXISTS` means the first repo's
    embed builds it on populated data; later repos insert into the existing
    index incrementally.
    """
    await session.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} "
            "ON entity_embedding USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
    )
    await session.commit()
