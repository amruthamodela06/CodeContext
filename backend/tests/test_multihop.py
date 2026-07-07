"""Tests for Slice 5f -- multi-hop traversal + embedding rerank + the
``retrieve_entities`` orchestrator. Per-stage unit-ish coverage; the
full end-to-end with classifier + LLM lands in Slice 5g's /query test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.multihop import rerank_by_embedding, traverse_outbound
from app.models import (
    CodeChunk,
    Commit,
    EntityEdge,
    EntityEmbedding,
    File,
    Issue,
    PullRequest,
    Repo,
)

pytestmark = pytest.mark.usefixtures("_clean_db")


# --- Fixture helper: a small "chunk -> commit -> pr -> issue" graph -------


async def _build_fixture_graph(session: AsyncSession) -> dict[str, int]:
    """Seed a deterministic graph for traversal tests. Returns id mapping."""
    repo = Repo(owner="o", name="r", default_branch="main")
    session.add(repo)
    await session.commit()

    file = File(repo_id=repo.id, path="x.py", size_bytes=10, language="Python")
    session.add(file)
    await session.commit()

    chunk = CodeChunk(
        repo_id=repo.id,
        file_id=file.id,
        chunk_type="function",
        name="f",
        start_line=1,
        end_line=3,
        content="def f(): pass",
        language="Python",
    )
    commit = Commit(repo_id=repo.id, sha="a" * 40, message="Add f()")
    pr = PullRequest(
        repo_id=repo.id,
        number=1,
        title="add f",
        body="implements f",
        state="merged",
        created_at=datetime.now(UTC),
    )
    issue = Issue(
        repo_id=repo.id,
        number=10,
        title="need f",
        body="we want f",
        state="closed",
        created_at=datetime.now(UTC),
    )
    session.add_all([chunk, commit, pr, issue])
    await session.commit()

    edges = [
        EntityEdge(
            repo_id=repo.id,
            source_type="chunk",
            source_id=chunk.id,
            target_type="commit",
            target_id=commit.id,
            edge_type="introduced_by",
        ),
        EntityEdge(
            repo_id=repo.id,
            source_type="commit",
            source_id=commit.id,
            target_type="pr",
            target_id=pr.id,
            edge_type="part_of",
        ),
        EntityEdge(
            repo_id=repo.id,
            source_type="pr",
            source_id=pr.id,
            target_type="issue",
            target_id=issue.id,
            edge_type="references_issue",
        ),
    ]
    session.add_all(edges)
    await session.commit()
    return {
        "repo": repo.id,
        "chunk": chunk.id,
        "commit": commit.id,
        "pr": pr.id,
        "issue": issue.id,
    }


# --- traverse_outbound -----------------------------------------------------


async def test_traverse_reaches_commit_and_pr_at_depth_two(
    session: AsyncSession,
) -> None:
    """depth=2 reaches chunk -> commit (hop 1) -> pr (hop 2), but NOT issue
    (hop 3)."""
    ids = await _build_fixture_graph(session)
    rows = await traverse_outbound(session, ids["repo"], [ids["chunk"]], max_depth=2)
    types = {(t, i) for t, i, _ in rows}
    assert ("commit", ids["commit"]) in types
    assert ("pr", ids["pr"]) in types
    assert ("issue", ids["issue"]) not in types  # depth=3 -- out of range


async def test_traverse_depth_three_reaches_issue(session: AsyncSession) -> None:
    ids = await _build_fixture_graph(session)
    rows = await traverse_outbound(session, ids["repo"], [ids["chunk"]], max_depth=3)
    types = {(t, i) for t, i, _ in rows}
    assert ("issue", ids["issue"]) in types


async def test_traverse_respects_max_breadth(session: AsyncSession) -> None:
    """Seed a chunk that fans out to >breadth commits; only breadth are kept."""
    repo = Repo(owner="o", name="r", default_branch="main")
    session.add(repo)
    await session.commit()
    file = File(repo_id=repo.id, path="x.py", size_bytes=10, language="Python")
    session.add(file)
    await session.commit()
    chunk = CodeChunk(
        repo_id=repo.id,
        file_id=file.id,
        chunk_type="function",
        name="f",
        start_line=1,
        end_line=3,
        content="def f(): pass",
        language="Python",
    )
    session.add(chunk)
    await session.commit()

    commits = [Commit(repo_id=repo.id, sha=f"{i:040x}", message=f"c{i}") for i in range(15)]
    session.add_all(commits)
    await session.commit()
    session.add_all(
        [
            EntityEdge(
                repo_id=repo.id,
                source_type="chunk",
                source_id=chunk.id,
                target_type="commit",
                target_id=c.id,
                edge_type="introduced_by",
            )
            for c in commits
        ]
    )
    await session.commit()

    rows = await traverse_outbound(session, repo.id, [chunk.id], max_depth=1, max_breadth=10)
    assert len(rows) == 10  # breadth cap honored


async def test_traverse_empty_seeds_returns_empty(session: AsyncSession) -> None:
    rows = await traverse_outbound(session, 1, [], max_depth=2)
    assert rows == []


# --- rerank_by_embedding ---------------------------------------------------


async def test_rerank_orders_by_cosine_similarity(session: AsyncSession) -> None:
    """Seed two commit embeddings; the one closer to the query vector
    must rank first."""
    ids = await _build_fixture_graph(session)

    # Two unit-length 384-dim vectors with very different first components.
    # The query is closer to vec_a; vec_a should rank first.
    vec_a = [1.0] + [0.0] * 383
    vec_b = [0.0, 1.0] + [0.0] * 382
    query = [1.0] + [0.0] * 383

    # Reuse the fixture's commit as one entity; add a second commit.
    second = Commit(repo_id=ids["repo"], sha="b" * 40, message="other")
    session.add(second)
    await session.commit()

    session.add_all(
        [
            EntityEmbedding(
                repo_id=ids["repo"],
                entity_type="commit",
                entity_id=ids["commit"],
                embedding=vec_a,
                model_name="test",
                dimension=384,
            ),
            EntityEmbedding(
                repo_id=ids["repo"],
                entity_type="commit",
                entity_id=second.id,
                embedding=vec_b,
                model_name="test",
                dimension=384,
            ),
        ]
    )
    await session.commit()

    candidates = [("commit", ids["commit"]), ("commit", second.id)]
    ranked = await rerank_by_embedding(session, ids["repo"], candidates, query, top_k=2)
    assert [t for t, _, _ in ranked] == ["commit", "commit"]
    # First (closer) commit ranks higher; second cosine score is lower.
    assert ranked[0][1] == ids["commit"]
    assert ranked[0][2] > ranked[1][2]


async def test_rerank_drops_candidates_without_embeddings(
    session: AsyncSession,
) -> None:
    ids = await _build_fixture_graph(session)
    # No embedding rows seeded -- rerank returns empty even though candidates
    # are listed.
    candidates = [("commit", ids["commit"]), ("pr", ids["pr"])]
    ranked = await rerank_by_embedding(session, ids["repo"], candidates, [0.0] * 384, top_k=10)
    assert ranked == []


# --- retrieve_entities (orchestrator smoke) --------------------------------


class _StubRetriever:
    """Retriever stand-in. Returns a preset list of RetrievalResult
    pointers so tests don't need seeded embeddings + FTS. The .name is
    surfaced into the trace so we can assert on it.
    """

    def __init__(self, results, name="stub"):
        self._results = results
        self.name = name
        self.called_with: list[tuple] = []

    async def retrieve(self, session, repo_id, query, top_k, filters=None):
        self.called_with.append((repo_id, query, top_k, filters))
        return self._results


async def test_retrieve_entities_routes_out_of_scope_to_empty(
    session: AsyncSession,
) -> None:
    from app.retrieval import retrieve_entities

    class _NeverCalled:
        name = "never"

        async def retrieve(self, *args, **kwargs):
            raise AssertionError("flat retrieval should not run for out_of_scope")

    ctx, trace = await retrieve_entities(
        session,
        repo_id=1,
        query="recipe?",
        top_k=5,
        category="out_of_scope",
        retriever=_NeverCalled(),
    )
    assert ctx.chunks == [] and ctx.commits == []
    assert trace["category"] == "out_of_scope"


async def test_retrieve_entities_lookup_skips_graph_expansion(
    session: AsyncSession,
) -> None:
    """A `lookup` category returns chunks only; multi-hop is not invoked."""
    from app.retrieval import RetrievalResult, retrieve_entities

    # Seed a real chunk so _hydrate_chunks can join through -- the retriever
    # returns pointers, hydration reads content + file_path from the DB.
    ids = await _build_fixture_graph(session)
    stub = _StubRetriever(
        [
            RetrievalResult(
                entity_type="chunk",
                entity_id=ids["chunk"],
                score=0.9,
                score_breakdown={"vector_score": 0.9},
            )
        ],
        name="stub-vec",
    )

    ctx, trace = await retrieve_entities(
        session,
        repo_id=ids["repo"],
        query="where is f",
        top_k=5,
        category="lookup",
        retriever=stub,
    )
    assert len(ctx.chunks) == 1
    assert ctx.chunks[0].chunk_id == ids["chunk"]
    assert ctx.commits == [] and ctx.prs == [] and ctx.issues == []
    # No expansion was attempted -> trace should not have the multi-hop keys.
    assert "expansion_candidates" not in trace
    assert trace["seed_chunk_ids"] == [ids["chunk"]]
    # retrieval_mode surfaced from the retriever.
    assert trace["retrieval_mode"] == "stub-vec"
