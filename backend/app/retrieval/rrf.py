"""Reciprocal rank fusion. See ADR 0014.

The standard RRF formula::

    score(d) = sum over rankers r of  1 / (k + rank_r(d))

where ``rank_r(d)`` is 1-indexed and ``k`` is a smoothing constant
(60 is the literature default; the retrieval tuning knob is exposed
via the factory so Slice 7 can ablate).

Missing rankers contribute 0 -- an entity that appears in only one
ranker's top-N still fuses, just with the single ranker's contribution.
The implementation makes that explicit rather than relying on
"infinity gives 0" limits.
"""

from __future__ import annotations

from app.retrieval.protocol import EntityType

RankedList = tuple[str, list[tuple[EntityType, int]]]
FusedRow = tuple[EntityType, int, float, dict[str, float]]


def reciprocal_rank_fusion(
    ranked_lists: list[RankedList],
    *,
    k: int = 60,
) -> list[FusedRow]:
    """Fuse ``ranked_lists`` via RRF. Returns rows score-descending.

    Each input tuple is ``(ranker_name, items)`` where ``items`` is
    already sorted by descending relevance (index 0 is rank 1). The
    ranker_name is embedded in per-entity ``score_breakdown`` keys
    (e.g. ``bm25_rank`` / ``vector_rank``) so downstream debug panels
    can render the fusion transparently.

    ``k`` smooths high ranks -- larger ``k`` compresses the score gap
    between rank 1 and rank 100. RRF's power is that it's insensitive
    to the absolute scale of underlying scores (cosine vs. ts_rank_cd)
    because it operates on ranks alone.
    """
    scores: dict[tuple[EntityType, int], float] = {}
    breakdowns: dict[tuple[EntityType, int], dict[str, float]] = {}

    for ranker_name, ranked in ranked_lists:
        for rank_1based, (entity_type, entity_id) in enumerate(ranked, start=1):
            key = (entity_type, entity_id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank_1based)
            breakdowns.setdefault(key, {})[f"{ranker_name}_rank"] = float(rank_1based)

    for key, breakdown in breakdowns.items():
        breakdown["rrf_score"] = scores[key]

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [(et, eid, score, breakdowns[(et, eid)]) for (et, eid), score in ordered]
