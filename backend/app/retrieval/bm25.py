"""BM25Retriever -- Postgres full-text search over the four ``fts_tsv``
columns Slice 6a added. See ADR 0014.

One SELECT arm per entity type, joined by UNION ALL, then a final
score-descending order + limit. Each arm uses ``ts_rank_cd`` with
normalization=32 (divide by ``1 + log(length)``) so long chunks don't
artificially out-rank short function signatures.

Postgres re-parses the query with the same english config that
generated the source tsvectors, so lowercasing / stemming / stopwords
match on both sides. Bare fts_tsv IS NULL rows drop out of the match
naturally via the ``@@`` predicate.

Slice 6b populated the chunk FTS intermediates (fts_name / fts_doc /
fts_body); commits / PRs / issues have their tsvector filled by the
generated column expression from their existing text columns.

``RetrievalFilters.entity_types`` narrows which UNION arms run (skip
building the SQL for excluded types entirely -- cheaper than building
and discarding).
"""

from __future__ import annotations

from sqlalchemy import desc, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CodeChunk, Commit, Issue, PullRequest
from app.retrieval.protocol import EntityType, RetrievalFilters, RetrievalResult

_ARMS: list[tuple[EntityType, type]] = [
    ("chunk", CodeChunk),
    ("commit", Commit),
    ("pr", PullRequest),
    ("issue", Issue),
]

# ts_rank_cd normalization bit-mask. 32 = divide by 1 + log(document length).
# Compensates for the wide length disparity across chunk bodies vs. commit
# subjects; without it long chunks + long PR bodies dominate the ranking.
_RANK_NORM = 32


class BM25Retriever:
    """Pure BM25-ish search (Postgres ``ts_rank_cd``). Kept for Slice 7 ablation."""

    name = "bm25"

    async def retrieve(
        self,
        session: AsyncSession,
        repo_id: int,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalResult]:
        allowed = self._allowed_types(filters)
        if not allowed:
            return []

        q_ts = func.plainto_tsquery("english", query)

        arms = []
        for entity_type, model in _ARMS:
            if entity_type not in allowed:
                continue
            rank = func.ts_rank_cd(model.fts_tsv, q_ts, _RANK_NORM).label("rank")
            arms.append(
                select(
                    literal(entity_type).label("entity_type"),
                    model.id.label("entity_id"),
                    rank,
                ).where(
                    model.repo_id == repo_id,
                    model.fts_tsv.op("@@")(q_ts),
                )
            )

        if not arms:
            return []

        unioned = union_all(*arms).alias("bm25")
        stmt = (
            select(unioned.c.entity_type, unioned.c.entity_id, unioned.c.rank)
            .order_by(desc(unioned.c.rank))
            .limit(top_k)
        )
        rows = (await session.execute(stmt)).all()
        return [
            RetrievalResult(
                entity_type=et,
                entity_id=eid,
                score=float(rank),
                score_breakdown={"bm25_score": float(rank)},
            )
            for et, eid, rank in rows
        ]

    @staticmethod
    def _allowed_types(filters: RetrievalFilters | None) -> set[EntityType]:
        if filters is None or filters.entity_types is None:
            return {"chunk", "commit", "pr", "issue"}
        return set(filters.entity_types)
