"""HybridRetriever -- vector + BM25 in parallel, RRF-fused. See ADR 0014.

The default retrieval mode for Slice 6 onwards. Runs the two underlying
retrievers concurrently via ``asyncio.gather``, then applies reciprocal
rank fusion to combine their outputs on ranks alone (not raw scores,
which have different scales -- cosine vs. ts_rank_cd).

Tunables (candidate pool per retriever, RRF smoothing constant) are
constructor args so the factory in 6h can wire them from env vars for
Slice 7 ablation without touching this class.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.bm25 import BM25Retriever
from app.retrieval.protocol import RetrievalFilters, RetrievalResult, Retriever
from app.retrieval.rrf import reciprocal_rank_fusion
from app.retrieval.vector import VectorRetriever


class HybridRetriever:
    """Vector + BM25 fused by RRF."""

    name = "hybrid"

    def __init__(
        self,
        vector: Retriever | None = None,
        bm25: Retriever | None = None,
        *,
        candidate_n: int = 50,
        rrf_k: int = 60,
    ) -> None:
        self._vector: Retriever = vector or VectorRetriever()
        self._bm25: Retriever = bm25 or BM25Retriever()
        self._candidate_n = candidate_n
        self._rrf_k = rrf_k

    async def retrieve(
        self,
        session: AsyncSession,
        repo_id: int,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalResult]:
        vec_task = self._vector.retrieve(session, repo_id, query, self._candidate_n, filters)
        bm25_task = self._bm25.retrieve(session, repo_id, query, self._candidate_n, filters)
        vec_results, bm25_results = await asyncio.gather(vec_task, bm25_task)

        fused = reciprocal_rank_fusion(
            [
                ("vector", [(r.entity_type, r.entity_id) for r in vec_results]),
                ("bm25", [(r.entity_type, r.entity_id) for r in bm25_results]),
            ],
            k=self._rrf_k,
        )

        return [
            RetrievalResult(
                entity_type=et,
                entity_id=eid,
                score=score,
                score_breakdown=breakdown,
            )
            for et, eid, score, breakdown in fused[:top_k]
        ]
