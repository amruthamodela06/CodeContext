"""Tests for the Slice 6 Retriever concretes. This file currently covers
VectorRetriever (6d); BM25Retriever (6e), HybridRetriever (6f), and
RerankedRetriever (6g) add their own sections here as they land.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.fts import compute_chunk_fts
from app.models import (
    CodeChunk,
    Commit,
    EntityEmbedding,
    File,
    Issue,
    PullRequest,
    Repo,
)
from app.retrieval import (
    BM25Retriever,
    HybridRetriever,
    RetrievalFilters,
    VectorRetriever,
)

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


# --- BM25Retriever --------------------------------------------------------


async def _seed_bm25_repo(session: AsyncSession, *, owner: str = "o", name: str = "bm25") -> dict:
    """Seed a repo with one chunk / commit / pr / issue whose text
    contains distinct English terms so BM25 queries land deterministically.

    - chunk (syncify function): matches queries about 'syncify', 'async', 'sync'.
    - commit (bcrypt change): matches queries about 'bcrypt', 'password',
      'hash'.
    - pr (also syncify): matches 'syncify', 'anyio', 'sync'.
    - issue (bcrypt gap): matches 'bcrypt', 'passwords', 'plaintext'.
    """
    from datetime import UTC, datetime

    repo = Repo(owner=owner, name=name, default_branch="main")
    session.add(repo)
    await session.commit()

    f = File(repo_id=repo.id, path="auth.py", size_bytes=10, language="Python")
    session.add(f)
    await session.commit()

    chunk_src = (
        'def syncify(fn):\n    """Run coroutine synchronously via anyio."""\n    return fn\n'
    )
    fts_name, fts_doc, fts_body = compute_chunk_fts(
        name="syncify",
        parent_name=None,
        content=chunk_src,
        language="Python",
    )
    chunk = CodeChunk(
        repo_id=repo.id,
        file_id=f.id,
        chunk_type="function",
        name="syncify",
        start_line=1,
        end_line=3,
        content=chunk_src,
        language="Python",
        fts_name=fts_name,
        fts_doc=fts_doc,
        fts_body=fts_body,
    )
    commit = Commit(
        repo_id=repo.id,
        sha="a" * 40,
        message="Add bcrypt password hashing to auth",
    )
    pr = PullRequest(
        repo_id=repo.id,
        number=42,
        title="Add syncify helper",
        body="Wraps anyio to run async from sync context.",
        state="merged",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    issue = Issue(
        repo_id=repo.id,
        number=100,
        title="Passwords not hashed",
        body="We store plaintext -- switch to bcrypt.",
        state="closed",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add_all([chunk, commit, pr, issue])
    await session.commit()
    return {
        "repo": repo.id,
        "chunk": chunk.id,
        "commit": commit.id,
        "pr": pr.id,
        "issue": issue.id,
    }


async def test_bm25_retriever_finds_matches_across_types(session: AsyncSession) -> None:
    """'syncify' should hit both the chunk and the PR that mention it,
    and NOT the bcrypt commit/issue."""
    ids = await _seed_bm25_repo(session)
    r = BM25Retriever()
    results = await r.retrieve(session, ids["repo"], "syncify", top_k=10)
    hits = {(res.entity_type, res.entity_id) for res in results}
    assert ("chunk", ids["chunk"]) in hits
    assert ("pr", ids["pr"]) in hits
    assert ("commit", ids["commit"]) not in hits
    assert ("issue", ids["issue"]) not in hits


async def test_bm25_retriever_scores_descending(session: AsyncSession) -> None:
    ids = await _seed_bm25_repo(session)
    r = BM25Retriever()
    results = await r.retrieve(session, ids["repo"], "bcrypt password", top_k=10)
    scores = [res.score for res in results]
    assert scores == sorted(scores, reverse=True)
    assert all(res.score > 0 for res in results)
    assert all("bm25_score" in res.score_breakdown for res in results)


async def test_bm25_retriever_filters_to_chunks_only(session: AsyncSession) -> None:
    """entity_types={'chunk'} means the PR match on 'syncify' drops out."""
    ids = await _seed_bm25_repo(session)
    r = BM25Retriever()
    results = await r.retrieve(
        session,
        ids["repo"],
        "syncify",
        top_k=10,
        filters=RetrievalFilters(entity_types={"chunk"}),
    )
    assert [res.entity_type for res in results] == ["chunk"]


async def test_bm25_retriever_empty_filter_returns_empty(session: AsyncSession) -> None:
    ids = await _seed_bm25_repo(session)
    r = BM25Retriever()
    results = await r.retrieve(
        session,
        ids["repo"],
        "syncify",
        top_k=10,
        filters=RetrievalFilters(entity_types=set()),
    )
    assert results == []


async def test_bm25_retriever_no_matches_returns_empty(session: AsyncSession) -> None:
    """A query with no lexical hits should return zero rows (not raise)."""
    ids = await _seed_bm25_repo(session)
    r = BM25Retriever()
    results = await r.retrieve(session, ids["repo"], "quantum photosynthesis", top_k=10)
    assert results == []


async def test_bm25_retriever_top_k_bounds(session: AsyncSession) -> None:
    """top_k caps the union-of-arms result set."""
    ids = await _seed_bm25_repo(session)
    r = BM25Retriever()
    # 'bcrypt' matches commit + issue -- top_k=1 must trim to one.
    results = await r.retrieve(session, ids["repo"], "bcrypt", top_k=1)
    assert len(results) == 1


async def test_bm25_retriever_scopes_to_repo(session: AsyncSession) -> None:
    """A second repo's fts_tsv rows are invisible."""
    ids_a = await _seed_bm25_repo(session, owner="a", name="one")
    await _seed_bm25_repo(session, owner="b", name="two")  # noise; must not leak
    r = BM25Retriever()
    results_a = await r.retrieve(session, ids_a["repo"], "syncify", top_k=10)
    ids_seen = {(res.entity_type, res.entity_id) for res in results_a}
    # Every hit belongs to repo A.
    assert ids_seen == {("chunk", ids_a["chunk"]), ("pr", ids_a["pr"])}


# --- HybridRetriever ------------------------------------------------------


async def _seed_hybrid_repo(session: AsyncSession, *, owner: str = "o", name: str = "hyb") -> dict:
    """Seed a repo with BOTH populated FTS content AND embeddings on all
    four entity types, so HybridRetriever can exercise vector + BM25 in
    parallel. Uses the BM25 fixture text + the polymorphic unit vectors.
    """
    from datetime import UTC, datetime

    ids = await _seed_bm25_repo(session, owner=owner, name=name)

    # Layer embeddings on top. Vectors are chosen so a query aligned with
    # vec_chunk ranks chunk#1 in vector, and the BM25 half handles syncify
    # match separately.
    vec_chunk = [1.0] + [0.0] * 383
    vec_commit = [0.0, 1.0] + [0.0] * 382
    vec_pr = [0.0, 0.0, 1.0] + [0.0] * 381
    vec_issue = [0.0, 0.0, 0.0, 1.0] + [0.0] * 380
    session.add_all(
        [
            EntityEmbedding(
                repo_id=ids["repo"],
                entity_type="chunk",
                entity_id=ids["chunk"],
                embedding=vec_chunk,
                model_name="fake-embedder",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=ids["repo"],
                entity_type="commit",
                entity_id=ids["commit"],
                embedding=vec_commit,
                model_name="fake-embedder",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=ids["repo"],
                entity_type="pr",
                entity_id=ids["pr"],
                embedding=vec_pr,
                model_name="fake-embedder",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=ids["repo"],
                entity_type="issue",
                entity_id=ids["issue"],
                embedding=vec_issue,
                model_name="fake-embedder",
                dimension=384,
            ),
        ]
    )
    await session.commit()
    # Suppress unused-import warning; datetime is imported implicitly by helper.
    _ = datetime, UTC
    return ids


async def test_hybrid_retriever_fuses_vector_and_bm25(session: AsyncSession) -> None:
    """A syncify query returns entities matched by BM25 (chunk + PR) and
    also entities pulled in by vector rerank of the four embeddings. The
    fused list carries per-retriever ranks in score_breakdown."""
    ids = await _seed_hybrid_repo(session)
    r = HybridRetriever()
    results = await r.retrieve(session, ids["repo"], "syncify", top_k=10)

    # BM25 matches produced chunk + pr for 'syncify'. Vector produced all
    # four entities. Fusion must include everything vector saw at least.
    types_seen = {(res.entity_type, res.entity_id) for res in results}
    assert ("chunk", ids["chunk"]) in types_seen
    assert ("pr", ids["pr"]) in types_seen
    # score_breakdown surfaces which ranker contributed.
    top_breakdown = results[0].score_breakdown
    assert "rrf_score" in top_breakdown
    # At least one of vector_rank / bm25_rank must be present on the top hit.
    assert "vector_rank" in top_breakdown or "bm25_rank" in top_breakdown


async def test_hybrid_retriever_scores_descending(session: AsyncSession) -> None:
    ids = await _seed_hybrid_repo(session)
    r = HybridRetriever()
    results = await r.retrieve(session, ids["repo"], "syncify", top_k=10)
    scores = [res.score for res in results]
    assert scores == sorted(scores, reverse=True)


async def test_hybrid_retriever_passes_filters_through(session: AsyncSession) -> None:
    """entity_types filter must apply to BOTH underlying retrievers."""
    ids = await _seed_hybrid_repo(session)
    r = HybridRetriever()
    results = await r.retrieve(
        session,
        ids["repo"],
        "syncify",
        top_k=10,
        filters=RetrievalFilters(entity_types={"chunk"}),
    )
    assert {res.entity_type for res in results} == {"chunk"}


async def test_hybrid_retriever_top_k_bounds_after_fusion(session: AsyncSession) -> None:
    """candidate_n=50 per retriever, but top_k=1 must trim to one final row."""
    ids = await _seed_hybrid_repo(session)
    r = HybridRetriever(candidate_n=10, rrf_k=60)
    results = await r.retrieve(session, ids["repo"], "syncify", top_k=1)
    assert len(results) == 1


async def test_hybrid_retriever_tunable_rrf_k(session: AsyncSession) -> None:
    """Larger rrf_k compresses the score gap. Ordering is stable, scores shift."""
    ids = await _seed_hybrid_repo(session)
    small_k = await HybridRetriever(rrf_k=60).retrieve(session, ids["repo"], "syncify", top_k=10)
    large_k = await HybridRetriever(rrf_k=6000).retrieve(session, ids["repo"], "syncify", top_k=10)
    # Same rows in same order (ties broken deterministically by dict insertion).
    assert [(r.entity_type, r.entity_id) for r in small_k] == [
        (r.entity_type, r.entity_id) for r in large_k
    ]
    # Scores are strictly smaller under the larger k.
    assert small_k[0].score > large_k[0].score
