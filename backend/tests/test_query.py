"""Integration test for POST /query via the MockLLMProvider (LLM_PROVIDER=mock).

Drives the full Slice 4 pipeline end-to-end: ingest -> embed -> retrieve ->
prompt -> mock LLM stream -> parse -> validate -> resolve -> SSE payload. No
network, no real model. The mock's canned answer cites c1 (valid), c99
(invalid), and [chunk:none], so all three statuses are exercised.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from app import ingest

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
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    monkeypatch.setattr(ingest, "clone_repo", _fake_clone_factory(sample_repo))
    r = await client.post("/ingest", json={"url": "https://github.com/test/fixture"})
    assert r.status_code == 200, r.text
    repo = r.json()["repo"]
    er = await client.post(f"/repos/{repo['id']}/embed")
    assert er.status_code == 202, er.text
    # BackgroundTasks run within the request lifecycle under ASGITransport.
    sr = await client.get(f"/repos/{repo['id']}/embedding-status")
    assert sr.json()["embedding_status"] == "done", sr.text
    return repo


async def _collect_sse(client: AsyncClient, payload: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async with client.stream("POST", "/query", json=payload) as resp:
        assert resp.status_code == 200, await resp.aread()
        event: str | None = None
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith(":"):  # comment / keep-alive ping
                continue
            if line == "":
                if event is not None and data_lines:
                    events.append((event, json.loads("".join(data_lines))))
                event, data_lines = None, []
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
    return events


async def test_query_streams_tokens_and_resolves_citations(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _ingest_and_embed(client, sample_repo, monkeypatch)
    events = await _collect_sse(
        client,
        {"repo_id": repo["id"], "question": "How do I run async from sync?", "top_k": 5},
    )
    kinds = [e for e, _ in events]
    # Event order: sources first, then >=1 token, then citations, then done.
    assert kinds[0] == "sources"
    assert "token" in kinds
    assert "citations" in kinds
    assert kinds[-1] == "done"

    # Sources expose the retrieved chunks (the fixture yields 2 Python chunks).
    sources = dict(events)["sources"]["sources"]
    assert {s["display_id"] for s in sources} == {"c1", "c2"}
    assert all("content" in s for s in sources)

    # Streamed tokens reconstruct the mock answer.
    answer = "".join(d["text"] for e, d in events if e == "token")
    assert "syncify" in answer

    # Citations: c1 valid (SHA-pinned permalink), c99 invalid, none -> none.
    citations = {c["display_id"]: c for c in dict(events)["citations"]["citations"]}
    assert citations["c1"]["status"] == "valid"
    assert citations["c1"]["permalink"] == (
        f"https://github.com/test/fixture/blob/{repo['commit_sha']}/src/main.py"
        f"#L{citations['c1']['start_line']}-L{citations['c1']['end_line']}"
    )
    assert citations["c99"]["status"] == "invalid"
    assert citations["c99"]["permalink"] is None
    assert citations["none"]["status"] == "none"


async def test_query_non_streaming_fallback(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _ingest_and_embed(client, sample_repo, monkeypatch)
    r = await client.post(
        "/query",
        json={"repo_id": repo["id"], "question": "what is syncify?", "stream": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "syncify" in body["answer"]
    statuses = {c["display_id"]: c["status"] for c in body["citations"]}
    assert statuses["c1"] == "valid"
    assert statuses["c99"] == "invalid"
    assert len(body["sources"]) == 2


async def test_query_unknown_repo_404(client: AsyncClient) -> None:
    r = await client.post("/query", json={"repo_id": 999999, "question": "hi"})
    assert r.status_code == 404
