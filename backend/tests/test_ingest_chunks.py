"""Integration tests for the chunking endpoints + the /ingest auto-chunk hook.

These cover the wire-level behavior: ingest writes chunks, the file list
reports per-file chunk counts, the listing endpoint paginates and filters,
the single-chunk lookup works, the explicit POST /chunk endpoint is
idempotent, and 404 paths are correct.

clone_repo is monkey-patched to copy the vendored sample-repo fixture into
the ingestion service's tempdir; no network. The fixture's `src/main.py`
produces exactly two Python chunks (a `function` for `main` and a
`top_level_block` for the `__main__` guard).
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import api, ingest
from app.models import CodeChunk

pytestmark = pytest.mark.usefixtures("_clean_db")


def _fake_clone_factory(sample_repo: Path):
    def fake_clone(clone_url: str, dest: Path) -> str:
        shutil.copytree(sample_repo, dest)
        head = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return head.stdout.strip()

    return fake_clone


async def _ingest_fixture(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))
    response = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    assert response.status_code == 200, response.text
    return response.json()


# --- Auto-chunk on /ingest ------------------------------------------------


async def test_ingest_auto_chunks_python(
    client: AsyncClient,
    sample_repo: Path,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]

    result = await session.execute(select(CodeChunk).where(CodeChunk.repo_id == repo_id))
    chunks = list(result.scalars().all())
    assert len(chunks) == 2
    assert {c.chunk_type for c in chunks} == {"function", "top_level_block"}
    function_chunk = next(c for c in chunks if c.chunk_type == "function")
    assert function_chunk.name == "main"
    assert function_chunk.language == "Python"
    assert function_chunk.is_async is False


async def test_file_list_reports_per_file_chunk_count(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    main_py = next(f for f in data["files"] if f["path"] == "src/main.py")
    assert main_py["chunk_count"] == 2
    # Markdown / JSON / TypeScript files have no chunker (or a stub) → 0
    non_py = [f for f in data["files"] if not f["path"].endswith(".py")]
    assert all(f["chunk_count"] == 0 for f in non_py)


# --- GET /repos/{repo_id}/chunks ------------------------------------------


async def test_list_chunks_returns_summary(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]

    r = await client.get(f"/repos/{repo_id}/chunks")
    assert r.status_code == 200
    body = r.json()
    assert body["repo_id"] == repo_id
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["summary"]["by_type"] == {"function": 1, "top_level_block": 1}
    assert body["summary"]["by_language"] == {"Python": 2}


async def test_list_chunks_filters_by_chunk_type(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]

    r = await client.get(f"/repos/{repo_id}/chunks?chunk_type=function")
    body = r.json()
    assert body["total"] == 1
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["chunk_type"] == "function"


async def test_list_chunks_filters_by_language(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]

    matching = await client.get(f"/repos/{repo_id}/chunks?language=Python")
    assert matching.json()["total"] == 2

    nonmatching = await client.get(f"/repos/{repo_id}/chunks?language=Rust")
    assert nonmatching.json()["total"] == 0


async def test_list_chunks_pagination(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]

    first = await client.get(f"/repos/{repo_id}/chunks?limit=1&offset=0")
    assert first.json()["total"] == 2
    assert len(first.json()["chunks"]) == 1

    second = await client.get(f"/repos/{repo_id}/chunks?limit=1&offset=1")
    assert len(second.json()["chunks"]) == 1

    # Disjoint pages.
    assert first.json()["chunks"][0]["id"] != second.json()["chunks"][0]["id"]

    past_end = await client.get(f"/repos/{repo_id}/chunks?limit=1&offset=10")
    assert past_end.json()["chunks"] == []


# --- GET /chunks/{chunk_id} ----------------------------------------------


async def test_get_single_chunk(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]
    listing = await client.get(f"/repos/{repo_id}/chunks?chunk_type=function")
    chunk_id = listing.json()["chunks"][0]["id"]

    r = await client.get(f"/chunks/{chunk_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == chunk_id
    assert body["name"] == "main"
    assert "def main" in body["content"]


# --- POST /repos/{repo_id}/chunk -----------------------------------------


async def test_post_chunk_reclones_and_is_idempotent(
    client: AsyncClient,
    sample_repo: Path,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _ingest_fixture(client, sample_repo, monkeypatch)
    repo_id = data["repo"]["id"]

    # The POST /chunk endpoint imports `clone_repo` into api.py's namespace, so
    # the patch on `ingest.clone_repo` from _ingest_fixture doesn't apply here.
    monkeypatch.setattr(api, "clone_repo", _fake_clone_factory(sample_repo))

    first = await client.post(f"/repos/{repo_id}/chunk")
    assert first.status_code == 200
    assert first.json() == {"repo_id": repo_id, "chunk_count": 2}

    # Re-run: same result, no duplicate rows.
    second = await client.post(f"/repos/{repo_id}/chunk")
    assert second.json()["chunk_count"] == 2

    total = await session.scalar(
        select(func.count(CodeChunk.id)).where(CodeChunk.repo_id == repo_id)
    )
    assert total == 2


# --- 404 paths ------------------------------------------------------------


async def test_404_unknown_repo_chunks(client: AsyncClient) -> None:
    r = await client.get("/repos/99999/chunks")
    assert r.status_code == 404


async def test_404_unknown_chunk(client: AsyncClient) -> None:
    r = await client.get("/chunks/99999")
    assert r.status_code == 404


async def test_404_post_chunk_unknown_repo(client: AsyncClient) -> None:
    r = await client.post("/repos/99999/chunk")
    assert r.status_code == 404
