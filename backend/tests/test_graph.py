"""Tests for Slice 5c -- blame + PR parsing + graph orchestrator stages.

The two unit modules (pr_parsing, blame.parse) get deep coverage with no DB.
The orchestrator stages run against the test DB: part_of and pr_issue_links
don't need a clone (pure SQL); introduced_by uses the vendored sample-repo
fixture so blame has real git history to walk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as app_db
from app.graph.blame import _parse_porcelain
from app.graph.orchestrator import (
    _stage_introduced_by,
    _stage_part_of,
    _stage_pr_issue_links,
)
from app.graph.pr_parsing import extract_closing_issue_numbers
from app.models import (
    CodeChunk,
    Commit,
    EntityEdge,
    File,
    Issue,
    PullRequest,
    Repo,
)

pytestmark = pytest.mark.usefixtures("_clean_db")


# --- pr_parsing (no DB) -----------------------------------------------------


def test_pr_parsing_catches_all_keyword_variants():
    txt = "This PR fixes #1, closes #2, and resolves #3."
    assert extract_closing_issue_numbers(txt) == {1, 2, 3}


def test_pr_parsing_is_case_insensitive():
    assert extract_closing_issue_numbers("Fixes #42", "CLOSES #43") == {42, 43}


def test_pr_parsing_ignores_bare_mentions():
    # Bare `#N` (no keyword) is a mention, not a close-reference.
    assert extract_closing_issue_numbers("See #99 for context, unlike #100.") == set()


def test_pr_parsing_handles_separators():
    # Realistic separators between keyword and #N: colon, space, multiple
    # spaces. Letters between (e.g. "closed-by #8") are correctly rejected.
    assert extract_closing_issue_numbers("fix: #7", "Closes  #8") == {7, 8}
    assert extract_closing_issue_numbers("closed-by #99") == set()


def test_pr_parsing_dedupes_across_title_and_body():
    assert extract_closing_issue_numbers("fixes #5", "also fixes #5") == {5}


def test_pr_parsing_handles_none():
    assert extract_closing_issue_numbers(None, None) == set()


# --- blame.parse (no DB, no git) --------------------------------------------


def test_parse_porcelain_extracts_line_to_sha_map():
    # Minimal porcelain block: header with sha + final-line; one content line.
    sha = "a" * 40
    output = (
        f"{sha} 1 1 2\n"
        "author Jane\n"
        "author-mail <jane@example.com>\n"
        "summary first commit\n"
        "filename foo.py\n"
        "\tprint('hello')\n"
        f"{sha} 2 2\n"
        "author Jane\n"
        "filename foo.py\n"
        "\tprint('world')\n"
    )
    mapping = _parse_porcelain(output)
    assert mapping == {1: sha, 2: sha}


def test_parse_porcelain_ignores_non_header_lines():
    # Make sure tabbed content lines and key/value header lines aren't
    # mistaken for header-with-sha lines.
    sha = "b" * 40
    output = (
        f"{sha} 1 1 1\n"
        "author X\n"
        "\tnot a header even if it contains 40 chars of garbage 0123456789012345\n"
    )
    assert _parse_porcelain(output) == {1: sha}


# --- Stage 2: commit -[part_of]-> pr ---------------------------------------


async def test_stage_part_of_links_pr_merge_commit(session: AsyncSession) -> None:
    repo = Repo(owner="o", name="r", default_branch="main")
    session.add(repo)
    await session.commit()

    commit = Commit(repo_id=repo.id, sha="abc123" + "0" * 34, message="m")
    pr = PullRequest(
        repo_id=repo.id,
        number=1,
        title="t",
        state="merged",
        created_at=_now(),
        merge_commit_sha=commit.sha,
    )
    session.add_all([commit, pr])
    await session.commit()

    count = await _stage_part_of(session, repo.id)
    assert count == 1

    edge = await session.scalar(
        select(EntityEdge).where(EntityEdge.repo_id == repo.id, EntityEdge.edge_type == "part_of")
    )
    assert edge is not None
    assert edge.source_type == "commit"
    assert edge.source_id == commit.id
    assert edge.target_type == "pr"
    assert edge.target_id == pr.id


# --- Stage 3: pr -[references_issue]-> issue + issue -[closed_by]-> pr -----


async def test_stage_pr_issue_links_writes_both_edges_and_closing_pr_number(
    session: AsyncSession,
) -> None:
    repo = Repo(owner="o", name="r", default_branch="main")
    session.add(repo)
    await session.commit()

    issue = Issue(
        repo_id=repo.id,
        number=42,
        title="auth uses MD5",
        body="bad",
        state="closed",
        created_at=_now(),
    )
    merged_pr = PullRequest(
        repo_id=repo.id,
        number=234,
        title="switch auth to bcrypt",
        body="closes #42",
        state="merged",
        created_at=_now(),
    )
    open_pr = PullRequest(
        repo_id=repo.id,
        number=235,
        title="WIP",
        body="references #42 (no keyword)",
        state="open",
        created_at=_now(),
    )
    # PR pointing at a nonexistent issue: edge should NOT be created.
    bad_pr = PullRequest(
        repo_id=repo.id,
        number=236,
        title="fixes #9999",
        state="merged",
        created_at=_now(),
    )
    session.add_all([issue, merged_pr, open_pr, bad_pr])
    await session.commit()

    ref_count, closed_count = await _stage_pr_issue_links(session, repo.id)
    assert ref_count == 1  # only merged_pr -> issue 42
    assert closed_count == 1  # closed_by inverse

    # References edge present.
    ref_edge = await session.scalar(
        select(EntityEdge).where(
            EntityEdge.repo_id == repo.id, EntityEdge.edge_type == "references_issue"
        )
    )
    assert ref_edge is not None
    assert ref_edge.source_id == merged_pr.id and ref_edge.target_id == issue.id

    # Inverse closed_by edge present (only because merged_pr is merged).
    closed_edge = await session.scalar(
        select(EntityEdge).where(EntityEdge.repo_id == repo.id, EntityEdge.edge_type == "closed_by")
    )
    assert closed_edge is not None
    assert closed_edge.source_id == issue.id and closed_edge.target_id == merged_pr.id

    # Issue.closing_pr_number populated from the merged PR.
    await session.refresh(issue)
    assert issue.closing_pr_number == 234


# --- Stage 1: chunk -[introduced_by]-> commit (uses fixture clone) ---------


async def test_stage_introduced_by_writes_edges_with_stub_commit(
    session: AsyncSession, sample_repo: Path
) -> None:
    """Run blame on the vendored fixture; orchestrator stubs the unknown
    commit and writes a chunk->commit edge with the blame line in metadata.
    """
    repo = Repo(owner="test", name="fixture", default_branch="main")
    session.add(repo)
    await session.commit()

    file = File(repo_id=repo.id, path="src/main.py", size_bytes=100, language="Python")
    session.add(file)
    await session.commit()
    chunk = CodeChunk(
        repo_id=repo.id,
        file_id=file.id,
        chunk_type="function",
        name="main",
        start_line=1,
        end_line=4,
        content="def main(): pass",
        language="Python",
    )
    session.add(chunk)
    await session.commit()

    n_edges = await _stage_introduced_by(session, repo.id, sample_repo)
    assert n_edges == 1

    edge = await session.scalar(select(EntityEdge).where(EntityEdge.repo_id == repo.id))
    assert edge is not None
    assert edge.edge_type == "introduced_by"
    assert edge.source_type == "chunk" and edge.source_id == chunk.id
    assert edge.target_type == "commit"
    assert edge.edge_metadata.get("blame_line") == 1

    # The target commit was stub-inserted (no prior row existed).
    commit = await session.scalar(select(Commit).where(Commit.id == edge.target_id))
    assert commit is not None
    assert len(commit.sha) == 40
    assert commit.repo_id == repo.id


# --- Endpoint --------------------------------------------------------------


async def test_trigger_build_graph_returns_202_and_flips_status(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST returns 202 + sets graph_status=in_progress. The background task
    itself is patched to a no-op so the test doesn't try to clone over the
    network (build_graph would re-clone the repo)."""
    async with app_db.SessionLocal() as s:
        repo = Repo(owner="o", name="r", default_branch="main")
        s.add(repo)
        await s.commit()
        repo_id = repo.id

    # Patch the symbol the endpoint imported (not the orchestrator module).
    from app import api as api_mod

    async def _noop(*args, **kwargs):
        return {}

    monkeypatch.setattr(api_mod, "build_graph", _noop)

    r = await client.post(f"/repos/{repo_id}/build-graph")
    assert r.status_code == 202, r.text
    assert r.json() == {"repo_id": repo_id, "graph_status": "in_progress"}

    s_resp = await client.get(f"/repos/{repo_id}/graph-status")
    body = s_resp.json()
    # No edges in the DB and the no-op background task didn't flip to done,
    # so status is still in_progress.
    assert body["introduced_by_count"] == 0
    assert body["part_of_count"] == 0


# --- helpers ----------------------------------------------------------------


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
