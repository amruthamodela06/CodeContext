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

import app.db as app_db
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

    # Sources event is now typed: per-type lists (chunks / commits / prs / issues).
    # The fixture yields 2 Python chunks; no history is seeded, so the other
    # lists are empty.
    sources = dict(events)["sources"]
    assert {s["display_id"] for s in sources["chunks"]} == {"c1", "c2"}
    assert all("content" in s for s in sources["chunks"])
    assert sources["commits"] == [] and sources["prs"] == [] and sources["issues"] == []

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
    assert len(body["chunks"]) == 2
    # Slice 5g: classifier output surfaced in the response.
    assert body["category"] in {"lookup", "architectural", "historical_why", "impact"}
    assert "classifier" in body["trace"]


async def test_query_unknown_repo_404(client: AsyncClient) -> None:
    r = await client.post("/query", json={"repo_id": 999999, "question": "hi"})
    assert r.status_code == 404


async def test_query_out_of_scope_short_circuits(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """classification_override='out_of_scope' must skip the LLM entirely and
    return the canned response with category surfaced in the trace."""
    repo = await _ingest_and_embed(client, sample_repo, monkeypatch)
    r = await client.post(
        "/query",
        json={
            "repo_id": repo["id"],
            "question": "What's the best pasta recipe?",
            "stream": False,
            "classification_override": "out_of_scope",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"] == "out_of_scope"
    assert "don't see anything in this repo" in body["answer"]
    assert body["citations"] == []
    assert body["chunks"] == []  # no retrieval ran
    assert body["trace"]["classifier"]["method"] == "override"


async def test_query_classification_override_routes_to_historical_why(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """classification_override='historical_why' triggers multi-hop expansion
    even on a question the keyword classifier would route elsewhere. With no
    history seeded, the expansion produces zero candidates and the chunks-only
    path takes over -- but the category + trace must still reflect the choice."""
    repo = await _ingest_and_embed(client, sample_repo, monkeypatch)
    r = await client.post(
        "/query",
        json={
            "repo_id": repo["id"],
            "question": "Find the syncify function",  # would normally classify as lookup
            "stream": False,
            "classification_override": "historical_why",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"] == "historical_why"
    assert body["trace"]["classifier"]["method"] == "override"
    # Multi-hop was attempted -- seed_chunk_ids populated by retrieve_entities.
    assert "seed_chunk_ids" in body["trace"]


# --- Slice 5i: full end-to-end /query historical_why pipeline -------------


async def _seed_full_graph(repo_id: int) -> dict:
    """Seed a repo with chunks + commits + PRs + issues + edges + embeddings
    so the historical_why pipeline can run end-to-end against a known graph.

    Returns the id mapping the test asserts against. Uses the test's
    SessionLocal (the fixture's session is request-scoped and would close
    before /query runs its own session).
    """
    from datetime import UTC, datetime

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

    async with app_db.SessionLocal() as s:
        repo = await s.get(Repo, repo_id)
        # SHA-pinned permalinks come from this.
        repo.commit_sha = "abcdef0" + "0" * 33
        await s.commit()

        from sqlalchemy import select

        file = (await s.execute(select(File).where(File.repo_id == repo_id).limit(1))).scalar_one()
        chunk_ids = [
            c.id
            for c in (await s.execute(select(CodeChunk).where(CodeChunk.repo_id == repo_id)))
            .scalars()
            .all()
        ]

        # Seed a commit, PR, issue + edges + embeddings.
        commit = Commit(
            repo_id=repo_id,
            sha="c" * 40,
            author_name="Jane",
            authored_at=datetime.now(UTC),
            message="Add syncify helper",
        )
        pr = PullRequest(
            repo_id=repo_id,
            number=234,
            title="Add syncify",
            body="Implements syncify per #189",
            state="merged",
            created_at=datetime.now(UTC),
            merged_at=datetime.now(UTC),
            merge_commit_sha="c" * 40,
        )
        issue = Issue(
            repo_id=repo_id,
            number=189,
            title="Need sync wrapper",
            body="We need a way to call async from sync.",
            state="closed",
            created_at=datetime.now(UTC),
        )
        s.add_all([commit, pr, issue])
        await s.commit()

        # Graph: chunks -> commit -> PR -> issue.
        edges = [
            EntityEdge(
                repo_id=repo_id,
                source_type="chunk",
                source_id=chunk_ids[0],
                target_type="commit",
                target_id=commit.id,
                edge_type="introduced_by",
            ),
            EntityEdge(
                repo_id=repo_id,
                source_type="commit",
                source_id=commit.id,
                target_type="pr",
                target_id=pr.id,
                edge_type="part_of",
            ),
            EntityEdge(
                repo_id=repo_id,
                source_type="pr",
                source_id=pr.id,
                target_type="issue",
                target_id=issue.id,
                edge_type="references_issue",
            ),
        ]
        s.add_all(edges)
        await s.commit()

        # Embeddings on every non-chunk entity so the reranker has something
        # to score. Chunks were embedded by _ingest_and_embed already.
        unit = [1.0] + [0.0] * 383
        s.add_all(
            [
                EntityEmbedding(
                    repo_id=repo_id,
                    entity_type="commit",
                    entity_id=commit.id,
                    embedding=unit,
                    model_name="fake-embedder",
                    dimension=384,
                ),
                EntityEmbedding(
                    repo_id=repo_id,
                    entity_type="pr",
                    entity_id=pr.id,
                    embedding=unit,
                    model_name="fake-embedder",
                    dimension=384,
                ),
                EntityEmbedding(
                    repo_id=repo_id,
                    entity_type="issue",
                    entity_id=issue.id,
                    embedding=unit,
                    model_name="fake-embedder",
                    dimension=384,
                ),
            ]
        )
        await s.commit()
        return {
            "repo": repo_id,
            "file": file.id,
            "chunk_ids": chunk_ids,
            "commit": commit.id,
            "commit_sha": commit.sha,
            "pr": pr.id,
            "pr_number": pr.number,
            "issue": issue.id,
            "issue_number": issue.number,
        }


class _CustomAnswerProvider:
    """LLMProvider stub that returns a fixed answer text. Used to drive
    /query with a known answer string carrying all four typed citation
    types, so the parse + validate + resolve chain can be asserted on."""

    def __init__(self, answer: str) -> None:
        self._answer = answer

    @property
    def name(self) -> str:
        return "stub-custom-answer"

    async def generate(self, messages, **opts):
        from app.llm.protocol import GenResult

        return GenResult(text=self._answer)

    async def generate_stream(self, messages, **opts):
        # Yield in two chunks so the streaming-accumulation path is exercised.
        mid = len(self._answer) // 2
        yield self._answer[:mid]
        yield self._answer[mid:]


async def test_query_historical_why_full_pipeline(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The big end-to-end: ingest -> embed -> seed graph + per-type embeddings
    + historical_why override -> stub LLM returns an answer citing all four
    typed entities. Assert the entire pipeline: classifier trace, multi-hop
    expansion + rerank counts, typed citations validated, per-type
    permalinks on the SSE sources payload (the bug da3baca fixed)."""
    repo = await _ingest_and_embed(client, sample_repo, monkeypatch)
    ids = await _seed_full_graph(repo["id"])

    answer = (
        "syncify was added to allow sync-from-async calls [chunk:c1]. "
        "The PR [pr:p1] introduced it, citing [issue:i1] as the motivation. "
        "The change landed in [commit:m1]. The original issue is also tracked "
        "but the link [chunk:c99] is malformed."
    )
    from app import api as api_mod

    monkeypatch.setattr(api_mod, "get_llm_provider", lambda: _CustomAnswerProvider(answer))

    events = await _collect_sse(
        client,
        {
            "repo_id": repo["id"],
            "question": "Why was syncify added?",
            "stream": True,
            "classification_override": "historical_why",
        },
    )
    by_event = dict(events)

    # --- Sources event: typed dict, every entity carries a permalink (da3baca).
    # depth=2 reaches chunk -> commit (hop 1) -> pr (hop 2). Issues sit at
    # hop 3 from a chunk and are NOT in the multi-hop context (ADR 0012);
    # they'd arrive via flat retrieval or the `closed_by` inverse instead.
    sources = by_event["sources"]
    assert {s["display_id"] for s in sources["chunks"]} == {"c1", "c2"}
    assert [c["display_id"] for c in sources["commits"]] == ["m1"]
    assert [p["display_id"] for p in sources["prs"]] == ["p1"]
    assert sources["issues"] == []  # depth=2 doesn't reach hop 3

    # Permalinks are present and well-formed per type.
    assert "/blob/" in sources["chunks"][0]["permalink"]
    assert (
        sources["commits"][0]["permalink"]
        == f"https://github.com/test/fixture/commit/{ids['commit_sha']}"
    )
    assert (
        sources["prs"][0]["permalink"] == f"https://github.com/test/fixture/pull/{ids['pr_number']}"
    )

    # --- Citations event: typed resolution + trace populated.
    cit_evt = by_event["citations"]
    by_key = {(c["entity_type"], c["display_id"]): c for c in cit_evt["citations"]}
    assert by_key[("chunk", "c1")]["status"] == "valid"
    assert by_key[("commit", "m1")]["status"] == "valid"
    assert by_key[("commit", "m1")]["commit_sha"] == ids["commit_sha"]
    assert by_key[("pr", "p1")]["status"] == "valid"
    assert by_key[("pr", "p1")]["pr_number"] == ids["pr_number"]
    # i1 was cited by the model but the issue isn't in the depth=2 context
    # -> validator must flag it invalid (mechanical citation discipline).
    assert by_key[("issue", "i1")]["status"] == "invalid"
    assert by_key[("issue", "i1")]["permalink"] is None
    assert by_key[("chunk", "c99")]["status"] == "invalid"

    trace = cit_evt["trace"]
    assert trace["classifier"]["method"] == "override"
    assert trace["category"] == "historical_why"
    assert trace["expansion_candidates"] >= 1  # the commit (then PR via depth=2)
    assert trace["reranked_count"] >= 1


async def test_query_sources_carry_permalinks_for_uncited_entities(
    client: AsyncClient, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for da3baca: every retrieved entity in the SSE sources
    payload must have its own permalink, not just the ones the model cited.
    Uses a stub LLM that emits NO citations at all so no ResolvedCitation
    rows are produced; the source permalinks must still be there."""
    repo = await _ingest_and_embed(client, sample_repo, monkeypatch)
    ids = await _seed_full_graph(repo["id"])

    from app import api as api_mod

    monkeypatch.setattr(
        api_mod,
        "get_llm_provider",
        lambda: _CustomAnswerProvider("Generic answer, no citations at all."),
    )
    events = await _collect_sse(
        client,
        {
            "repo_id": repo["id"],
            "question": "Why?",
            "stream": True,
            "classification_override": "historical_why",
        },
    )
    sources = dict(events)["sources"]
    # Citations payload is empty -- model wrote no [type:id] tokens.
    assert dict(events)["citations"]["citations"] == []
    # ...but every retrieved entity still carries its own permalink.
    for entity_list in (sources["chunks"], sources["commits"], sources["prs"], sources["issues"]):
        for e in entity_list:
            assert e["permalink"], f"missing permalink on {e.get('display_id')}"
    assert sources["commits"][0]["permalink"].endswith(ids["commit_sha"])
