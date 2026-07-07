"""RerankedRetriever -- wraps another Retriever and reorders its top-N
via a cross-encoder. See ADR 0014.

Bi-encoder retrievers (vector, BM25) score query and passage
independently, then match by proximity in that shared space. A
cross-encoder reads both together through its trained relevance head
and can spot mismatches a bi-encoder can't (e.g. a passage that
lexically matches but semantically contradicts the query). The cost
is real: ~50 ms per pair on CPU with ``bge-reranker-base``, so ~1--2 s
for the default N=20. That's why reranking is opt-in via
``RETRIEVAL_MODE=hybrid_rerank`` -- the default deployed system runs
hybrid without it.

Model: ``BAAI/bge-reranker-base`` via ``sentence-transformers``'s
``CrossEncoder`` (already in the dep tree from Slice 3's embedder).
Cached under the standard HuggingFace directory; ~280 MB on first
use. Loading is lazy -- the model isn't touched unless the retriever
is actually invoked.

Content composition per type follows the ADR 0014 recommendation:
content only, no metadata prefixes. The cross-encoder was trained on
generic ``(query, passage)`` pairs, and prefixing headers like
``# file.py::function()`` puts it in a distribution it wasn't trained
on. Long chunks are head-truncated at the 512-token context boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CodeChunk, Commit, Issue, PullRequest
from app.retrieval.protocol import (
    EntityType,
    RetrievalFilters,
    RetrievalResult,
    Retriever,
)

log = logging.getLogger(__name__)


class _CrossEncoderProtocol(Protocol):
    """The subset of ``sentence_transformers.CrossEncoder`` we use.

    Defined here so tests can inject a lightweight stub without loading the
    real model. Real CrossEncoder's ``predict`` is sync + CPU-bound; the
    retriever wraps it in ``asyncio.to_thread`` to keep the event loop free.
    """

    def predict(self, sentences: list[tuple[str, str]]) -> list[float]: ...


class RerankedRetriever:
    """Runs ``inner`` for the top-``input_n``, cross-encoder rescores,
    returns top-``top_k``."""

    name = "hybrid_rerank"

    def __init__(
        self,
        inner: Retriever,
        *,
        input_n: int = 20,
        model_name: str = "BAAI/bge-reranker-base",
        model: _CrossEncoderProtocol | None = None,
    ) -> None:
        self._inner = inner
        self._input_n = input_n
        self._model_name = model_name
        self._model: _CrossEncoderProtocol | None = model  # DI or lazy-load

    async def retrieve(
        self,
        session: AsyncSession,
        repo_id: int,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalResult]:
        candidates = await self._inner.retrieve(session, repo_id, query, self._input_n, filters)
        if not candidates:
            return []

        texts = await _hydrate_texts(session, candidates)
        model = self._get_model()
        pairs = [(query, text) for text in texts]
        # predict() is sync CPU work; hop off the event loop.
        scores = await asyncio.to_thread(model.predict, pairs)

        combined = list(zip(candidates, scores, strict=True))
        combined.sort(key=lambda x: x[1], reverse=True)
        return [
            RetrievalResult(
                entity_type=c.entity_type,
                entity_id=c.entity_id,
                score=float(s),
                # Preserve the inner's breakdown (RRF ranks, vector_score,
                # bm25_score, etc.) so the debug panel shows the full journey.
                score_breakdown={**c.score_breakdown, "rerank_score": float(s)},
            )
            for c, s in combined[:top_k]
        ]

    def _get_model(self) -> _CrossEncoderProtocol:
        if self._model is not None:
            return self._model
        # Lazy import so ``import app.retrieval`` doesn't pull ML deps for
        # callers that will only use vector / bm25 / hybrid modes.
        from sentence_transformers import CrossEncoder

        log.info("loading cross-encoder %s (CPU, max_length=512)", self._model_name)
        self._model = CrossEncoder(self._model_name, device="cpu", max_length=512)
        return self._model


async def _hydrate_texts(session: AsyncSession, results: list[RetrievalResult]) -> list[str]:
    """Fetch each entity's rerank-input text in one query per type.

    Returns texts aligned to ``results``' original order. Content per type
    matches ADR 0014:

    - chunk: raw code content (may be truncated at the 512-token boundary
      by the reranker's tokenizer)
    - commit: full message (subject + body)
    - pr / issue: ``title + "\\n\\n" + body`` (body may be empty)
    """
    by_type: dict[EntityType, list[int]] = {
        "chunk": [],
        "commit": [],
        "pr": [],
        "issue": [],
    }
    for r in results:
        by_type[r.entity_type].append(r.entity_id)

    texts: dict[tuple[EntityType, int], str] = {}
    if by_type["chunk"]:
        rows = (
            await session.execute(
                select(CodeChunk.id, CodeChunk.content).where(CodeChunk.id.in_(by_type["chunk"]))
            )
        ).all()
        texts.update((("chunk", cid), content) for cid, content in rows)
    if by_type["commit"]:
        rows = (
            await session.execute(
                select(Commit.id, Commit.message).where(Commit.id.in_(by_type["commit"]))
            )
        ).all()
        texts.update((("commit", cid), msg or "") for cid, msg in rows)
    if by_type["pr"]:
        rows = (
            await session.execute(
                select(PullRequest.id, PullRequest.title, PullRequest.body).where(
                    PullRequest.id.in_(by_type["pr"])
                )
            )
        ).all()
        texts.update((("pr", pid), f"{title}\n\n{body or ''}") for pid, title, body in rows)
    if by_type["issue"]:
        rows = (
            await session.execute(
                select(Issue.id, Issue.title, Issue.body).where(Issue.id.in_(by_type["issue"]))
            )
        ).all()
        texts.update((("issue", iid), f"{title}\n\n{body or ''}") for iid, title, body in rows)

    return [texts.get((r.entity_type, r.entity_id), "") for r in results]
