"""Unit tests for reciprocal rank fusion. Pure Python math -- no DB."""

from __future__ import annotations

import pytest

from app.retrieval.rrf import reciprocal_rank_fusion


def test_rrf_empty_input_returns_empty() -> None:
    assert reciprocal_rank_fusion([]) == []


def test_rrf_single_ranker_preserves_order() -> None:
    """One ranker: fused order matches the input; scores match 1/(k+rank)."""
    items = [("chunk", 1), ("chunk", 2), ("chunk", 3)]
    fused = reciprocal_rank_fusion([("solo", items)], k=60)
    assert [(et, eid) for et, eid, _, _ in fused] == items
    # scores match 1/(k+rank), rank is 1-indexed
    assert fused[0][2] == pytest.approx(1 / 61)
    assert fused[1][2] == pytest.approx(1 / 62)
    assert fused[2][2] == pytest.approx(1 / 63)


def test_rrf_two_rankers_sum_contributions() -> None:
    """Entity appearing in both rankers accumulates both contributions.
    Verifies the standard RRF formula end-to-end.

    Setup: entity A ranks 1 in vector, 3 in bm25.
           entity B ranks 3 in vector, 1 in bm25.
    Both should score identically after fusion.
    """
    vector = [("chunk", 1), ("chunk", 2), ("chunk", 3)]  # A=1, C=2, B=3
    bm25 = [("chunk", 3), ("chunk", 2), ("chunk", 1)]  # B=1, C=2, A=3
    fused = reciprocal_rank_fusion(
        [("vector", vector), ("bm25", bm25)],
        k=60,
    )
    by_id = {(et, eid): (score, bd) for et, eid, score, bd in fused}

    expected_a = 1 / 61 + 1 / 63
    expected_b = 1 / 63 + 1 / 61
    expected_c = 1 / 62 + 1 / 62

    assert by_id[("chunk", 1)][0] == pytest.approx(expected_a)
    assert by_id[("chunk", 3)][0] == pytest.approx(expected_b)
    assert by_id[("chunk", 2)][0] == pytest.approx(expected_c)
    # A and B tied; C different.
    assert by_id[("chunk", 1)][0] == pytest.approx(by_id[("chunk", 3)][0])


def test_rrf_missing_from_one_ranker_still_scores() -> None:
    """Entity only in vector's list contributes vector's rank alone --
    the missing ranker adds 0. Distinguishes 'missed by one' from 'in both'."""
    only_in_vector = [("chunk", 99)]
    both = [("chunk", 1)]
    fused = reciprocal_rank_fusion(
        [
            ("vector", both + only_in_vector),  # rank 1 = chunk 1, rank 2 = chunk 99
            ("bm25", both),  # rank 1 = chunk 1 only
        ],
        k=60,
    )
    by_id = {(et, eid): score for et, eid, score, _ in fused}
    # chunk 1: 1/61 + 1/61 = 2/61
    # chunk 99: 1/62 (vector only)
    assert by_id[("chunk", 1)] == pytest.approx(2 / 61)
    assert by_id[("chunk", 99)] == pytest.approx(1 / 62)
    # chunk 1 ranks higher because it appears in both.
    assert fused[0][:2] == ("chunk", 1)
    assert fused[1][:2] == ("chunk", 99)


def test_rrf_breakdown_carries_per_ranker_ranks_and_score() -> None:
    """The breakdown surface downstream renders is {ranker}_rank + rrf_score."""
    fused = reciprocal_rank_fusion(
        [
            ("vector", [("chunk", 1)]),
            ("bm25", [("chunk", 1)]),
        ],
        k=60,
    )
    _, _, score, breakdown = fused[0]
    assert breakdown["vector_rank"] == 1.0
    assert breakdown["bm25_rank"] == 1.0
    assert breakdown["rrf_score"] == pytest.approx(score)


def test_rrf_k_smooths_score_gap() -> None:
    """Larger k compresses the score gap between rank 1 and rank 10."""
    items = [("chunk", i) for i in range(1, 11)]
    fused_k60 = reciprocal_rank_fusion([("solo", items)], k=60)
    fused_k600 = reciprocal_rank_fusion([("solo", items)], k=600)

    ratio_k60 = fused_k60[0][2] / fused_k60[9][2]
    ratio_k600 = fused_k600[0][2] / fused_k600[9][2]
    # Larger k -> ratio closer to 1 (less spread).
    assert ratio_k60 > ratio_k600


def test_rrf_handles_cross_type_entities() -> None:
    """RRF operates on (entity_type, entity_id) pairs -- a chunk with id=1
    and a commit with id=1 are distinct entities that don't collide."""
    fused = reciprocal_rank_fusion(
        [
            ("vector", [("chunk", 1), ("commit", 1)]),
            ("bm25", [("commit", 1), ("chunk", 1)]),
        ],
        k=60,
    )
    assert len(fused) == 2
    keys = {(et, eid) for et, eid, _, _ in fused}
    assert keys == {("chunk", 1), ("commit", 1)}
