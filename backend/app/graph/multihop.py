"""Multi-hop graph expansion + embedding rerank. See ADR 0012.

Given a list of seed chunk IDs (top-k flat retrieval results), walk
entity_edge to find reachable commits / PRs / issues within a bounded
depth and breadth. Rerank the expanded candidate set by cosine similarity
to the query vector. Conservative defaults (depth=2, breadth=10) per the
Slice 5 plan -- each hop multiplies candidate-set size, so we start
small and tune via the Slice 7 eval.

Depth=2 reaches `chunk -> commit -> pr`. It does NOT reach issues; those
arrive via flat retrieval matching the question against issue bodies
directly, or via the inverse closed_by edge when an issue closes a PR
that the chain already includes.
"""

from __future__ import annotations

from sqlalchemy import bindparam, text, tuple_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Integer

from app.models import EntityEmbedding

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_BREADTH = 10  # per seed chunk

# Recursive CTE: anchor with the seed chunks at depth 0, then follow
# outbound entity_edge rows to depth N. The window function caps the
# number of distinct expanded entities per seed (max_breadth) so a
# fan-out hot-spot doesn't balloon the candidate set.
_TRAVERSAL_SQL = text(
    """
WITH RECURSIVE expansion AS (
    -- Cast the literal 'chunk' to varchar(16) so its type matches the
    -- recursive term's entity_edge.target_type column. Postgres rejects
    -- recursive CTEs whose columns differ across anchor + recursive.
    SELECT
        seed.id                 AS origin_chunk,
        'chunk'::varchar(16)    AS entity_type,
        seed.id                 AS entity_id,
        0                       AS depth
    FROM unnest(:seed_ids) AS seed(id)

    UNION ALL

    SELECT
        e.origin_chunk,
        ee.target_type,
        ee.target_id,
        e.depth + 1
    FROM expansion e
    JOIN entity_edge ee
      ON ee.repo_id     = :repo_id
     AND ee.source_type = e.entity_type
     AND ee.source_id   = e.entity_id
    WHERE e.depth < :max_depth
),
dedup AS (
    SELECT origin_chunk, entity_type, entity_id, MIN(depth) AS depth
    FROM expansion
    WHERE depth > 0
    GROUP BY origin_chunk, entity_type, entity_id
),
ranked AS (
    SELECT
        entity_type, entity_id, depth,
        ROW_NUMBER() OVER (PARTITION BY origin_chunk ORDER BY depth, entity_id) AS rn
    FROM dedup
)
SELECT DISTINCT entity_type, entity_id, MIN(depth) AS depth
FROM ranked
WHERE rn <= :max_breadth
GROUP BY entity_type, entity_id
"""
).bindparams(bindparam("seed_ids", type_=ARRAY(Integer)))


async def traverse_outbound(
    session: AsyncSession,
    repo_id: int,
    seed_chunk_ids: list[int],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_breadth: int = DEFAULT_MAX_BREADTH,
) -> list[tuple[str, int, int]]:
    """Return distinct (entity_type, entity_id, depth) reachable from any
    seed chunk within max_depth hops, capped at max_breadth per seed.

    The seed chunks themselves are NOT included in the result -- callers
    keep them on the flat-retrieval side of the response.
    """
    if not seed_chunk_ids:
        return []
    rows = (
        await session.execute(
            _TRAVERSAL_SQL,
            {
                "seed_ids": list(seed_chunk_ids),
                "repo_id": repo_id,
                "max_depth": max_depth,
                "max_breadth": max_breadth,
            },
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def rerank_by_embedding(
    session: AsyncSession,
    repo_id: int,
    candidates: list[tuple[str, int]],
    query_vec: list[float],
    top_k: int,
) -> list[tuple[str, int, float]]:
    """Score (entity_type, entity_id) candidates against the query vector
    and return the top-k as (entity_type, entity_id, similarity), sorted
    by descending similarity.

    Candidates without an embedding row (e.g. stub commits with no text)
    are silently dropped -- can't rank what we can't embed.
    """
    if not candidates:
        return []
    distance = EntityEmbedding.embedding.cosine_distance(query_vec).label("distance")
    from sqlalchemy import select  # local import to avoid module-level noise

    rows = (
        await session.execute(
            select(EntityEmbedding.entity_type, EntityEmbedding.entity_id, distance)
            .where(
                EntityEmbedding.repo_id == repo_id,
                tuple_(EntityEmbedding.entity_type, EntityEmbedding.entity_id).in_(candidates),
            )
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [(t, i, 1.0 - float(d)) for t, i, d in rows]
