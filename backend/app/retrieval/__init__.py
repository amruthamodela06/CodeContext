"""Retrieval package. Owns the Slice 6 provider seam (Retriever) plus
Slice 5's cross-domain multi-hop orchestrator (retrieve_entities).

The orchestrator + protocol both live here so that call sites (api.py,
tests) can keep the flat ``from app.retrieval import ...`` shape.
The concrete Retrievers arrive in 6d-6g and are wired via the factory
in 6h.
"""

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
    "reciprocal_rank_fusion",
    "retrieve_entities",
]
