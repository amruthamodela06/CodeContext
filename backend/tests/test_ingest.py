"""Integration tests for POST /ingest and GET /repos/{owner}/{name}/files.

Uses the vendored sample-repo fixture (ADR 0003) rather than going to network.
The real `clone_repo` is monkey-patched per test to copy from the fixture into the
ingestion service's tmpdir; everything else (URL parsing, filesystem walk, filter,
language detection, DB upsert, response serialization) runs unmodified.
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ingest
from app.models import File, Repo

# Truncate repo + file before every test in this module.
pytestmark = pytest.mark.usefixtures("_clean_db")


def _fake_clone_factory(sample_repo: Path) -> Callable[[str, Path], str]:
    """Build a stand-in for ingest.clone_repo that uses the local fixture."""

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


async def test_ingest_returns_filtered_file_list(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))

    response = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    assert response.status_code == 200
    data = response.json()

    assert data["repo"]["owner"] == "test"
    assert data["repo"]["name"] == "fixture"
    assert data["repo"]["default_branch"] == "main"

    paths = {f["path"] for f in data["files"]}
    # Source files kept
    assert paths == {"README.md", "package.json", "src/lib.ts", "src/main.py"}
    # Junk filtered (lockfile, binary ext, skip-dir contents, .git internals)
    assert "package-lock.json" not in paths
    assert "logo.png" not in paths
    assert not any(p.startswith("vendor/") for p in paths)
    assert not any(p.startswith(".git/") for p in paths)

    assert data["file_count"] == 4


async def test_ingest_records_language_and_size(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))

    response = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    by_path = {f["path"]: f for f in response.json()["files"]}

    assert by_path["src/main.py"]["language"] == "Python"
    assert by_path["src/lib.ts"]["language"] == "TypeScript"
    assert by_path["README.md"]["language"] == "Markdown"
    assert by_path["package.json"]["language"] == "JSON"

    # Sizes are non-zero and match the on-disk file (sanity check that we're
    # not just emitting zeros).
    for f in by_path.values():
        assert f["size_bytes"] > 0


async def test_ingest_persists_to_db(
    client: AsyncClient,
    sample_repo: Path,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))

    await client.post("/ingest", json={"url": "https://github.com/test/fixture"})

    repo_count = await session.scalar(select(func.count()).select_from(Repo))
    file_count = await session.scalar(select(func.count()).select_from(File))
    assert repo_count == 1
    assert file_count == 4


async def test_get_files_returns_ingested_data(
    client: AsyncClient,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))

    ingest_resp = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    get_resp = await client.get("/repos/test/fixture/files")

    assert get_resp.status_code == 200
    assert get_resp.json()["file_count"] == ingest_resp.json()["file_count"]
    assert {f["path"] for f in get_resp.json()["files"]} == {
        f["path"] for f in ingest_resp.json()["files"]
    }


async def test_re_ingest_replaces_via_cascade(
    client: AsyncClient,
    sample_repo: Path,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))

    first = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    second = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})

    # New repo row inserted; old gone.
    assert second.json()["repo"]["id"] != first.json()["repo"]["id"]

    # Exactly one repo, exactly the new file set, no orphaned files.
    repo_count = await session.scalar(select(func.count()).select_from(Repo))
    file_count = await session.scalar(select(func.count()).select_from(File))
    assert repo_count == 1
    assert file_count == 4


async def test_400_on_invalid_url(client: AsyncClient) -> None:
    response = await client.post("/ingest", json={"url": "git@github.com:owner/repo.git"})
    assert response.status_code == 400


async def test_404_on_unknown_repo(client: AsyncClient) -> None:
    response = await client.get("/repos/never/ingested/files")
    assert response.status_code == 404
