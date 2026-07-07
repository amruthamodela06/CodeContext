"""Retriever protocol + result / filter models. See ADR 0014.

Third instance of the provider pattern (after Embedder in Slice 3 and
LLMProvider in Slice 4) -- shape mostly settled, one change: retrievers
return *pointers* (entity_type + entity_id + score), not hydrated rows.
Hydration to CitedChunk / CitedCommit / CitedPR / CitedIssue stays in
Slice 5's retrieve_entities orchestrator where per-type materialization
+ typed citations already live.

The Slice 6 retrievers:
- VectorRetriever  (6d) -- pgvector cosine over entity_embedding
- BM25Retriever    (6e) -- Postgres FTS over the four fts_tsv columns
- HybridRetriever  (6f) -- vector + bm25 in parallel, RRF-fused
- RerankedRetriever(6g) -- wraps any Retriever + cross-encoder rerank

RETRIEVAL_MODE env var selects the concrete via the factory (6h). All
implementations honor the same contract:

- ``retrieve()`` is repo-scoped, top-k bounded, and returns results
  sorted by descending score.
- Missing entity types are handled by returning zero results for that
  type -- never raise.
- ``score_breakdown`` is a debugging surface, not a public contract:
  its keys vary by retriever (``vector_score`` / ``bm25_rank`` /
  ``rrf_score`` / ``rerank_score``). The API layer forwards it into
  Slice 5's debug trace.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

EntityType = Literal["chunk", "commit", "pr", "issue"]


class RetrievalFilters(BaseModel):
    """Optional constraints applied inside ``Retriever.retrieve``.

    ``entity_types``: restrict the search to a subset (e.g. ``{"chunk"}`` for
    Slice 5's ``lookup`` classifier category, which only wants code and skips
    history). ``None`` (the default) means "any type".
    """

    model_config = ConfigDict(frozen=True)

    entity_types: set[EntityType] | None = Field(default=None)


class RetrievalResult(BaseModel):
    """One retrieved entity + its score + a per-component breakdown.

    Callers hydrate the pointer (``entity_type``, ``entity_id``) via Slice 5's
    ``retrieve_entities`` before rendering. Scores are mode-dependent -- for
    ``vector`` they're cosine similarities in [0, 1]; for ``bm25`` they're
    ``ts_rank_cd`` values; for ``hybrid`` they're RRF sums; for
    ``hybrid_rerank`` they're the reranker's logit output. Sort order across
    a returned list is always score-descending regardless of mode.
    """

    model_config = ConfigDict(frozen=True)

    entity_type: EntityType
    entity_id: int
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class Retriever(Protocol):
    """Retriever seam. All concretes implement ``retrieve``."""

    name: str

    async def retrieve(
        self,
        session: AsyncSession,
        repo_id: int,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalResult]:
        """Return up to ``top_k`` results, score-descending.

        - ``query`` is raw natural-language text; retrievers that need an
          embedding compute it internally so the caller stays uniform.
        - ``filters`` narrows the candidate set (e.g. entity types).
          ``None`` = no filter.
        """
        ...
