"""Integration tests for the embedding pipeline + vector search.

Fast path uses the deterministic FakeEmbedder (EMBEDDING_PROVIDER=mock, set in
conftest). The sample-repo fixture's src/main.py yields two chunks (the `main`
function + the `__main__` guard). The function chunk contains "print", "hello",
"fixture", so a query with those tokens must rank it first under the
FakeEmbedder's hashing-bag-of-words cosine ordering.

FastAPI BackgroundTasks run within the ASGI request lifecycle, so by the time
`await client.post("/embed")` returns, the background embed has completed —
the tests can assert on the resulting state directly.

The real bge-small model is exercised by one slow test gated behind RUN_SLOW.
"""

import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ingest
from app.models import EntityEmbedding

pytestmark = pytest.mark.usefixtures("_clean_db")


def _fake_clone_factory(sample_repo: Path):
    def fake_clone(clone_url: str, dest: Path) -> ingest.CloneResult:
        shutil.copytree(sample_repo, dest)
        branch = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return ingest.CloneResult(
            default_branch=branch.stdout.strip(), commit_sha=sha.stdout.strip()
        )

    return fake_clone


async def _ingest_and_embed(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))
    ingest_resp = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    assert ingest_resp.status_code == 200, ingest_resp.text
    repo_id = ingest_resp.json()["repo"]["id"]

    embed_resp = await client.post(f"/repos/{repo_id}/embed")
    assert embed_resp.status_code == 202, embed_resp.text
    assert embed_resp.json() == {"repo_id": repo_id, "embedding_status": "in_progress"}
    return repo_id


# --- Embed pipeline -------------------------------------------------------


async def test_embed_creates_one_vector_per_chunk(
    client: AsyncClient,
    sample_repo: Path,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_id = await _ingest_and_embed(client, sample_repo, monkeypatch)

    count = await session.scalar(
        select(func.count(EntityEmbedding.id)).where(
            EntityEmbedding.repo_id == repo_id,
            EntityEmbedding.entity_type == "chunk",
        )
    )
    assert count == 2

    # Vectors carry the model identity + dimension.
    row = await session.scalar(
        select(EntityEmbedding).where(
            EntityEmbedding.repo_id == repo_id,
            EntityEmbedding.entity_type == "chunk",
        )
    )
    assert row.model_name == "fake-embedder"
    assert row.dimension == 384


async def test_embedding_status_reaches_done(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_id = await _ingest_and_embed(client, sample_repo, monkeypatch)

    r = await client.get(f"/repos/{repo_id}/embedding-status")
    assert r.status_code == 200
    body = r.json()
    assert body["embedding_status"] == "done"
    assert body["embedding_progress"] == 1.0
    assert body["chunks_total"] == 2
    assert body["chunks_embedded"] == 2


async def test_reembed_is_idempotent(
    client: AsyncClient,
    sample_repo: Path,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_id = await _ingest_and_embed(client, sample_repo, monkeypatch)
    # Embed again — should replace, not duplicate.
    second = await client.post(f"/repos/{repo_id}/embed")
    assert second.status_code == 202

    count = await session.scalar(
        select(func.count(EntityEmbedding.id)).where(
            EntityEmbedding.repo_id == repo_id,
            EntityEmbedding.entity_type == "chunk",
        )
    )
    assert count == 2


# --- Search ---------------------------------------------------------------


async def test_search_ranks_token_overlap_first(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_id = await _ingest_and_embed(client, sample_repo, monkeypatch)

    r = await client.post(
        "/search",
        json={"repo_id": repo_id, "query": "print hello fixture", "top_k": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "print hello fixture"
    assert len(body["results"]) == 2

    top = body["results"][0]
    # The `main` function chunk shares the query tokens; it must rank first.
    assert top["file_path"] == "src/main.py"
    assert "print" in top["content"]
    assert top["chunk_type"] == "function"

    # Results are sorted by descending similarity.
    sims = [hit["similarity"] for hit in body["results"]]
    assert sims == sorted(sims, reverse=True)


async def test_search_respects_top_k(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_id = await _ingest_and_embed(client, sample_repo, monkeypatch)
    r = await client.post("/search", json={"repo_id": repo_id, "query": "main", "top_k": 1})
    assert len(r.json()["results"]) == 1


# --- 404 paths ------------------------------------------------------------


async def test_embed_404_unknown_repo(client: AsyncClient) -> None:
    r = await client.post("/repos/99999/embed")
    assert r.status_code == 404


# --- Slice 5d: orchestrator embeds commits / PRs / issues too --------------


async def test_embed_repo_widens_to_all_entity_types(session: AsyncSession) -> None:
    """Seed one row per non-chunk entity type; verify embed_repo populates
    entity_embedding with one row per type."""
    from datetime import UTC, datetime

    import app.db as app_db
    from app.embeddings.orchestrator import embed_repo
    from app.models import (
        CodeChunk,
        Commit,
        File,
        Issue,
        PullRequest,
        Repo,
    )

    repo = Repo(owner="o", name="r", default_branch="main")
    session.add(repo)
    await session.commit()

    file = File(repo_id=repo.id, path="x.py", size_bytes=10, language="Python")
    session.add(file)
    await session.commit()
    session.add_all(
        [
            CodeChunk(
                repo_id=repo.id,
                file_id=file.id,
                chunk_type="function",
                name="f",
                start_line=1,
                end_line=3,
                content="def f(): pass",
                language="Python",
            ),
            Commit(repo_id=repo.id, sha="a" * 40, message="Add f() helper"),
            PullRequest(
                repo_id=repo.id,
                number=1,
                title="add f()",
                body="adds f helper",
                state="merged",
                created_at=datetime.now(UTC),
            ),
            Issue(
                repo_id=repo.id,
                number=10,
                title="missing f",
                body="we need f",
                state="closed",
                created_at=datetime.now(UTC),
            ),
        ]
    )
    await session.commit()

    total = await embed_repo(repo.id, app_db.SessionLocal)
    assert total == 4  # one per entity type

    counts = dict(
        (
            await session.execute(
                select(EntityEmbedding.entity_type, func.count(EntityEmbedding.id))
                .where(EntityEmbedding.repo_id == repo.id)
                .group_by(EntityEmbedding.entity_type)
            )
        ).all()
    )
    assert counts == {"chunk": 1, "commit": 1, "pr": 1, "issue": 1}


async def test_embedding_status_reports_per_type_counts(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with no history yet, status surfaces the new per-type fields
    (zeros for commit/pr/issue) instead of breaking the schema."""
    repo_id = await _ingest_and_embed(client, sample_repo, monkeypatch)
    r = await client.get(f"/repos/{repo_id}/embedding-status")
    body = r.json()
    assert body["chunks_total"] == 2 and body["chunks_embedded"] == 2
    for key in ("commits", "prs", "issues"):
        assert body[f"{key}_total"] == 0
        assert body[f"{key}_embedded"] == 0


async def test_embedding_status_404_unknown_repo(client: AsyncClient) -> None:
    r = await client.get("/repos/99999/embedding-status")
    assert r.status_code == 404


async def test_search_404_unknown_repo(client: AsyncClient) -> None:
    r = await client.post("/search", json={"repo_id": 99999, "query": "x", "top_k": 5})
    assert r.status_code == 404


# --- Slow: real bge-small model -------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RUN_SLOW"),
    reason="real-model test; set RUN_SLOW=1 (downloads bge-small ~130MB on first run)",
)
def test_real_bge_small_semantic_ordering() -> None:
    """Semantically similar texts must score higher than dissimilar ones —
    the property naive vector search relies on. Uses the real model, so it's
    gated behind RUN_SLOW.
    """
    from app.embeddings.sentence_transformers import SentenceTransformersEmbedder

    embedder = SentenceTransformersEmbedder("BAAI/bge-small-en-v1.5")

    def cosine(a: list[float], b: list[float]) -> float:
        # bge vectors are normalized, so dot product == cosine similarity.
        return sum(x * y for x, y in zip(a, b, strict=True))

    query = embedder.embed_one("how do I read a file in python")
    similar = embedder.embed_one("open the file and read its contents line by line")
    dissimilar = embedder.embed_one("the weather today is sunny with a light breeze")

    assert embedder.dimension == 384
    assert math.isclose(cosine(query, query), 1.0, rel_tol=1e-4)
    assert cosine(query, similar) > cosine(query, dissimilar)
