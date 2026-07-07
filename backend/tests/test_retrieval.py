"""Tests for the Slice 6 Retriever concretes. This file currently covers
VectorRetriever (6d); BM25Retriever (6e), HybridRetriever (6f), and
RerankedRetriever (6g) add their own sections here as they land.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CodeChunk,
    Commit,
    EntityEmbedding,
    File,
    Issue,
    PullRequest,
    Repo,
)
from app.retrieval import RetrievalFilters, VectorRetriever

pytestmark = pytest.mark.usefixtures("_clean_db")


async def _seed_polymorphic_repo(
    session: AsyncSession, *, owner: str = "o", name: str = "r"
) -> dict:
    """Seed one repo with one chunk / commit / pr / issue, each with a
    distinct-enough embedding that ranking is deterministic. Returns id
    mapping the tests assert on.

    Vectors are unit basis vectors so cosine distance is 1 - dot; a
    query aligned with vec_chunk ranks the chunk first, etc.

    The (owner, name) override lets tests seed multiple repos in one
    session without tripping the uq_repo_owner_name constraint.
    """
    repo = Repo(owner=owner, name=name, default_branch="main")
    session.add(repo)
    await session.commit()

    f = File(repo_id=repo.id, path="x.py", size_bytes=10, language="Python")
    session.add(f)
    await session.commit()

    chunk = CodeChunk(
        repo_id=repo.id,
        file_id=f.id,
        chunk_type="function",
        name="f",
        start_line=1,
        end_line=1,
        content="def f(): pass\n",
        language="Python",
    )
    commit = Commit(repo_id=repo.id, sha="a" * 40, message="add f")
    pr = PullRequest(
        repo_id=repo.id,
        number=1,
        title="add f",
        body="implements f",
        state="merged",
        created_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
    )
    issue = Issue(
        repo_id=repo.id,
        number=10,
        title="need f",
        body="want f",
        state="closed",
        created_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
    )
    session.add_all([chunk, commit, pr, issue])
    await session.commit()

    # Four orthogonal-ish 384-dim vectors.
    vec_chunk = [1.0] + [0.0] * 383
    vec_commit = [0.0, 1.0] + [0.0] * 382
    vec_pr = [0.0, 0.0, 1.0] + [0.0] * 381
    vec_issue = [0.0, 0.0, 0.0, 1.0] + [0.0] * 380

    session.add_all(
        [
            EntityEmbedding(
                repo_id=repo.id,
                entity_type="chunk",
                entity_id=chunk.id,
                embedding=vec_chunk,
                model_name="fake-embedder",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=repo.id,
                entity_type="commit",
                entity_id=commit.id,
                embedding=vec_commit,
                model_name="fake-embedder",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=repo.id,
                entity_type="pr",
                entity_id=pr.id,
                embedding=vec_pr,
                model_name="fake-embedder",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=repo.id,
                entity_type="issue",
                entity_id=issue.id,
                embedding=vec_issue,
                model_name="fake-embedder",
                dimension=384,
            ),
        ]
    )
    await session.commit()
    return {
        "repo": repo.id,
        "chunk": chunk.id,
        "commit": commit.id,
        "pr": pr.id,
        "issue": issue.id,
    }


# --- VectorRetriever ------------------------------------------------------


async def test_vector_retriever_returns_all_types(session: AsyncSession) -> None:
    """No filter -> all four entity types are candidates."""
    ids = await _seed_polymorphic_repo(session)
    r = VectorRetriever()
    results = await r.retrieve(session, ids["repo"], "any query", top_k=10)
    assert {res.entity_type for res in results} == {"chunk", "commit", "pr", "issue"}
    assert len(results) == 4
    # Sorted by score descending.
    scores = [res.score for res in results]
    assert scores == sorted(scores, reverse=True)
    # score_breakdown carries vector_score + distance for debug surface.
    assert all("vector_score" in res.score_breakdown for res in results)
    assert all("distance" in res.score_breakdown for res in results)


async def test_vector_retriever_filters_to_chunks_only(session: AsyncSession) -> None:
    """RetrievalFilters(entity_types={'chunk'}) drops history -- what the
    Slice 5 'lookup' classifier category will pass in."""
    ids = await _seed_polymorphic_repo(session)
    r = VectorRetriever()
    results = await r.retrieve(
        session,
        ids["repo"],
        "q",
        top_k=10,
        filters=RetrievalFilters(entity_types={"chunk"}),
    )
    assert [res.entity_type for res in results] == ["chunk"]
    assert results[0].entity_id == ids["chunk"]


async def test_vector_retriever_filters_multiple_types(session: AsyncSession) -> None:
    ids = await _seed_polymorphic_repo(session)
    r = VectorRetriever()
    results = await r.retrieve(
        session,
        ids["repo"],
        "q",
        top_k=10,
        filters=RetrievalFilters(entity_types={"chunk", "commit"}),
    )
    assert {res.entity_type for res in results} == {"chunk", "commit"}


async def test_vector_retriever_empty_filter_returns_empty(session: AsyncSession) -> None:
    """An empty entity_types set means 'nothing' -- retriever must not
    silently ignore it. Distinguishes 'skip all types' from 'no filter'."""
    ids = await _seed_polymorphic_repo(session)
    r = VectorRetriever()
    results = await r.retrieve(
        session,
        ids["repo"],
        "q",
        top_k=10,
        filters=RetrievalFilters(entity_types=set()),
    )
    assert results == []


async def test_vector_retriever_top_k_bounds(session: AsyncSession) -> None:
    ids = await _seed_polymorphic_repo(session)
    r = VectorRetriever()
    results = await r.retrieve(session, ids["repo"], "q", top_k=2)
    assert len(results) == 2


async def test_vector_retriever_scopes_to_repo(session: AsyncSession) -> None:
    """A second repo's embeddings are invisible."""
    ids_a = await _seed_polymorphic_repo(session, owner="a", name="one")
    ids_b = await _seed_polymorphic_repo(session, owner="b", name="two")
    assert ids_a["repo"] != ids_b["repo"]

    r = VectorRetriever()
    results_a = await r.retrieve(session, ids_a["repo"], "q", top_k=10)
    entity_ids_a = {(res.entity_type, res.entity_id) for res in results_a}
    # Every entity id in repo A's result set belongs to repo A.
    for et, eid in entity_ids_a:
        if et == "chunk":
            assert eid == ids_a["chunk"]
        elif et == "commit":
            assert eid == ids_a["commit"]
        elif et == "pr":
            assert eid == ids_a["pr"]
        elif et == "issue":
            assert eid == ids_a["issue"]
