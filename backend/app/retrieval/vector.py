"""VectorRetriever -- pgvector cosine similarity over the polymorphic
``entity_embedding`` table. See ADR 0014.

Extends the Slice 3/4 chunk-only path (``api.retrieve_chunks``) to all
four entity types (chunk / commit / pr / issue). The single HNSW index
plus the polymorphic table shape from Slice 5d means one query ranks
across everything -- no per-type UNION, no separate embedders.
Constrain to specific types via ``RetrievalFilters.entity_types``.

The distance -> similarity conversion is ``similarity = 1 - distance``,
matching Slice 3's SearchResult convention so downstream consumers see
values in ``[0, 1]``.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.models import EntityEmbedding
from app.retrieval.protocol import RetrievalFilters, RetrievalResult


class VectorRetriever:
    """Pure vector search. Kept for the Slice 7 ablation baseline."""

    name = "vector"

    async def retrieve(
        self,
        session: AsyncSession,
        repo_id: int,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalResult]:
        embedder = get_embedder()
        query_vec = await asyncio.to_thread(embedder.embed_one, query)

        distance = EntityEmbedding.embedding.cosine_distance(query_vec).label("distance")
        stmt = (
            select(
                EntityEmbedding.entity_type,
                EntityEmbedding.entity_id,
                distance,
            )
            .where(EntityEmbedding.repo_id == repo_id)
            .order_by(distance)
            .limit(top_k)
        )
        if filters is not None and filters.entity_types is not None:
            # Empty set is intentional -- caller asked for nothing.
            if not filters.entity_types:
                return []
            stmt = stmt.where(EntityEmbedding.entity_type.in_(filters.entity_types))

        rows = (await session.execute(stmt)).all()
        return [
            RetrievalResult(
                entity_type=et,
                entity_id=eid,
                score=1.0 - float(dist),
                score_breakdown={
                    "vector_score": 1.0 - float(dist),
                    "distance": float(dist),
                },
            )
            for et, eid, dist in rows
        ]
