"""Repo-level embedding orchestrator. See ADR 0009 + 0013.

Slice 3 embedded only code chunks. Slice 5d widens the orchestrator to also
embed commits, PRs, and issues into the polymorphic entity_embedding table
(entity_type discriminator). Same job, four stages.

Runs as a FastAPI BackgroundTask with its own session (the request session is
closed by the time it runs). Each stage is idempotent: it deletes the repo's
prior embeddings *for its entity_type only* before re-inserting, so re-running
embed_repo after fresh history ingestion doesn't stomp the chunk embeddings
(or vice versa). The HNSW index is built once at the end across all rows.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.embeddings import get_embedder
from app.models import CodeChunk, Commit, EntityEmbedding, Issue, PullRequest, Repo

log = logging.getLogger(__name__)

_BATCH_SIZE = 32
_HNSW_INDEX = "entity_embedding_hnsw_cos"


def _batches(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


# ---- Per-entity-type text extractors --------------------------------------


async def _fetch_chunks(session: AsyncSession, repo_id: int) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(CodeChunk.id, CodeChunk.content).where(CodeChunk.repo_id == repo_id)
        )
    ).all()
    return [(r[0], r[1]) for r in rows if r[1]]


async def _fetch_commits(session: AsyncSession, repo_id: int) -> list[tuple[int, str]]:
    rows = (
        await session.execute(select(Commit.id, Commit.message).where(Commit.repo_id == repo_id))
    ).all()
    return [(r[0], r[1]) for r in rows if r[1] and r[1].strip()]


async def _fetch_prs(session: AsyncSession, repo_id: int) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(PullRequest.id, PullRequest.title, PullRequest.body).where(
                PullRequest.repo_id == repo_id
            )
        )
    ).all()
    return [(r[0], _join_title_body(r[1], r[2])) for r in rows if (r[1] or r[2])]


async def _fetch_issues(session: AsyncSession, repo_id: int) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(Issue.id, Issue.title, Issue.body).where(Issue.repo_id == repo_id)
        )
    ).all()
    return [(r[0], _join_title_body(r[1], r[2])) for r in rows if (r[1] or r[2])]


def _join_title_body(title: str | None, body: str | None) -> str:
    title = (title or "").strip()
    body = (body or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


# ---- Stage spec ------------------------------------------------------------


@dataclass(frozen=True)
class _StageSpec:
    entity_type: str
    fetch: Callable[[AsyncSession, int], Awaitable[list[tuple[int, str]]]]


_STAGES: tuple[_StageSpec, ...] = (
    _StageSpec("chunk", _fetch_chunks),
    _StageSpec("commit", _fetch_commits),
    _StageSpec("pr", _fetch_prs),
    _StageSpec("issue", _fetch_issues),
)


# ---- Main orchestrator ----------------------------------------------------


async def embed_repo(repo_id: int, session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Embed all available entities (chunks + commits + PRs + issues).

    Returns the total number of rows embedded across all stages. Stages with
    no rows (e.g. history not yet ingested) are no-ops. Encode failures
    within a batch are skipped (existing Slice 3 behavior); a stage that
    contributes zero successful batches doesn't fail the overall job as long
    as another stage produced rows. The job is marked `failed` only if every
    stage produced zero embeddings.
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

    total_embedded = 0
    stages_with_rows = 0
    per_stage_counts: dict[str, int] = {}

    for stage_idx, spec in enumerate(_STAGES):
        count = await _embed_stage(session_factory, embedder, repo_id, spec)
        per_stage_counts[spec.entity_type] = count
        total_embedded += count
        if count > 0:
            stages_with_rows += 1
        async with session_factory() as session:
            repo = await session.get(Repo, repo_id)
            if repo is not None:
                repo.embedding_progress = (stage_idx + 1) / len(_STAGES)
                await session.commit()

    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:
            return total_embedded
        if total_embedded == 0:
            # Nothing to embed across all stages — either an empty repo or
            # encode kept failing. Distinguish "no rows" (done) from "had
            # rows but encode failed" by re-checking if any stage had rows.
            any_rows = any(c > 0 for c in per_stage_counts.values())
            if not any_rows and stages_with_rows == 0:
                # All stages were empty — that's a legitimate done (no work).
                repo.embedding_status = "done"
            else:
                repo.embedding_status = "failed"
            repo.embedding_progress = 1.0
            await session.commit()
            return 0

        await _ensure_hnsw_index(session)
        repo.embedding_status = "done"
        repo.embedding_progress = 1.0
        await session.commit()

    return total_embedded


async def _embed_stage(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Any,
    repo_id: int,
    spec: _StageSpec,
) -> int:
    """One stage = fetch rows for an entity_type, delete prior embeddings
    for that type, batch-encode + insert. Returns count embedded.
    """
    async with session_factory() as session:
        rows = await spec.fetch(session, repo_id)
        if not rows:
            return 0
        # Idempotent re-embed for this entity_type only — leaves other types' rows.
        await session.execute(
            delete(EntityEmbedding).where(
                EntityEmbedding.repo_id == repo_id,
                EntityEmbedding.entity_type == spec.entity_type,
            )
        )
        await session.commit()

    embedded = 0
    for batch in _batches(rows, _BATCH_SIZE):
        ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]
        try:
            vectors = await asyncio.to_thread(embedder.embed_batch, texts)
        except Exception:
            log.exception(
                "embed batch failed (repo=%d type=%s size=%d); skipping",
                repo_id,
                spec.entity_type,
                len(batch),
            )
            continue
        async with session_factory() as session:
            session.add_all(
                EntityEmbedding(
                    entity_type=spec.entity_type,
                    entity_id=eid,
                    repo_id=repo_id,
                    embedding=vec,
                    model_name=embedder.name,
                    dimension=embedder.dimension,
                )
                for eid, vec in zip(ids, vectors, strict=True)
            )
            await session.commit()
        embedded += len(batch)
    return embedded


async def _ensure_hnsw_index(session: AsyncSession) -> None:
    """Create the HNSW cosine index if it doesn't exist (ADR 0009 + 0013).

    One index across all entity_embedding rows (post-Slice-5d) — the
    polymorphic widening doesn't change vector ops. `IF NOT EXISTS` keeps
    this idempotent across runs and across the chunk -> commit/pr/issue
    embedding waves.
    """
    await session.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} "
            "ON entity_embedding USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
    )
    await session.commit()
