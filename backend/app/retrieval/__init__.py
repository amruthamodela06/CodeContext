"""Retrieval package. Owns the Slice 6 provider seam (Retriever) plus
Slice 5's cross-domain multi-hop orchestrator (retrieve_entities).

The concrete retrievers (VectorRetriever / BM25Retriever /
HybridRetriever / RerankedRetriever) sit behind ``get_retriever()``,
which reads ``RETRIEVAL_MODE`` + related env from settings and returns
a cached singleton. Same shape as ``get_embedder()`` / ``get_llm_provider()``.
"""

from functools import cache

from app.config import get_settings
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.orchestrator import retrieve_entities
from app.retrieval.protocol import (
    EntityType,
    RetrievalFilters,
    RetrievalResult,
    Retriever,
)
from app.retrieval.reranked import RerankedRetriever
from app.retrieval.rrf import reciprocal_rank_fusion
from app.retrieval.vector import VectorRetriever

__all__ = [
    "BM25Retriever",
    "EntityType",
    "HybridRetriever",
    "RerankedRetriever",
    "RetrievalFilters",
    "RetrievalResult",
    "Retriever",
    "VectorRetriever",
    "get_retriever",
    "reciprocal_rank_fusion",
    "retrieve_entities",
]


@cache
def get_retriever() -> Retriever:
    """Returns the configured retriever, cached at first call.

    Mode selected by ``RETRIEVAL_MODE`` env:
      - ``vector``        : VectorRetriever  (Slice 3/4 baseline).
      - ``bm25``          : BM25Retriever    (Postgres FTS only).
      - ``hybrid``        : HybridRetriever  (parallel + RRF, default).
      - ``hybrid_rerank`` : RerankedRetriever wrapping HybridRetriever.

    Unknown modes fall back to ``hybrid`` with a log warning -- typos in
    the env shouldn't crash the app at startup; the eval harness in
    Slice 7 sets this per-run so silent fallback + a warn is friendlier
    than a hard fail.
    """
    settings = get_settings()
    mode = settings.retrieval_mode

    if mode == "vector":
        return VectorRetriever()
    if mode == "bm25":
        return BM25Retriever()

    # hybrid + hybrid_rerank both build a HybridRetriever base with the
    # tunable candidate_n / rrf_k.
    hybrid = HybridRetriever(
        candidate_n=settings.retrieval_candidate_n,
        rrf_k=settings.rrf_k,
    )
    if mode == "hybrid":
        return hybrid
    if mode == "hybrid_rerank":
        return RerankedRetriever(
            hybrid,
            input_n=settings.reranker_input_n,
            model_name=settings.reranker_model,
        )

    # Unknown -- log + fall back.
    import logging

    logging.getLogger(__name__).warning("unknown RETRIEVAL_MODE=%r; falling back to hybrid", mode)
    return hybrid
