"""Tests for Slice 5b — GraphQL persistence, the resumable orchestrator,
and the POST /repos/{repo_id}/ingest-history endpoint.

No live GitHub: the GraphQL client is replaced with a fake that returns
canned pages. The persistence helpers run against the real test DB so
ON CONFLICT and the polymorphic shape are exercised end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as app_db
from app.history.orchestrator import ingest_history
from app.history.persistence import (
    upsert_commits,
    upsert_issues,
    upsert_pull_requests,
)
from app.models import Commit, Issue, IssueComment, PRComment, Repo

pytestmark = pytest.mark.usefixtures("_clean_db")


# --- Fixture helpers -------------------------------------------------------


async def _make_repo(session: AsyncSession, owner: str = "o", name: str = "r") -> Repo:
    repo = Repo(owner=owner, name=name, default_branch="main")
    session.add(repo)
    await session.commit()
    return repo


def _commit_node(oid: str, message: str = "msg", *, authored: str = "2026-06-01T12:00:00Z") -> dict:
    return {
        "oid": oid,
        "message": message,
        "authoredDate": authored,
        "committedDate": authored,
        "author": {"name": "Jane", "email": "jane@example.com"},
        "additions": 10,
        "deletions": 2,
        "changedFilesIfAvailable": 1,
        "parents": {"nodes": [{"oid": "p1"}]},
    }


def _pr_node(
    number: int,
    *,
    body: str = "fixes #1",
    state: str = "MERGED",
    comments: list[dict] | None = None,
    reviews: list[dict] | None = None,
) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": body,
        "state": state,
        "author": {"login": "alice"},
        "createdAt": "2026-06-01T10:00:00Z",
        "updatedAt": "2026-06-01T11:00:00Z",
        "mergedAt": "2026-06-01T11:00:00Z" if state == "MERGED" else None,
        "closedAt": "2026-06-01T11:00:00Z" if state != "OPEN" else None,
        "mergeCommit": {"oid": "abc"} if state == "MERGED" else None,
        "baseRefName": "main",
        "headRefName": "feat/x",
        "additions": 20,
        "deletions": 5,
        "changedFiles": 3,
        "comments": {"nodes": comments or []},
        "reviews": {"nodes": reviews or []},
    }


def _issue_node(number: int, *, state: str = "CLOSED") -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "something is broken",
        "state": state,
        "author": {"login": "bob"},
        "createdAt": "2026-05-01T10:00:00Z",
        "updatedAt": "2026-06-01T10:00:00Z",
        "closedAt": "2026-06-01T10:00:00Z" if state == "CLOSED" else None,
        "labels": {"nodes": [{"name": "bug"}, {"name": "p1"}]},
        "comments": {
            "nodes": [
                {
                    "databaseId": 100 + number,
                    "body": "+1",
                    "author": {"login": "carol"},
                    "createdAt": "2026-05-02T10:00:00Z",
                },
            ]
        },
    }


# --- Persistence ----------------------------------------------------------


async def test_upsert_commits_writes_rows_and_is_idempotent(session: AsyncSession) -> None:
    repo = await _make_repo(session)
    nodes = [_commit_node("aaa", "first"), _commit_node("bbb", "second")]
    count = await upsert_commits(session, repo.id, nodes)
    await session.commit()
    assert count == 2
    total = await session.scalar(select(func.count()).select_from(Commit))
    assert total == 2

    # Re-running with an updated message updates in place (ON CONFLICT DO UPDATE).
    nodes2 = [_commit_node("aaa", "first-edited"), _commit_node("ccc", "third")]
    await upsert_commits(session, repo.id, nodes2)
    await session.commit()
    total = await session.scalar(select(func.count()).select_from(Commit))
    assert total == 3
    edited = await session.scalar(select(Commit).where(Commit.sha == "aaa"))
    assert edited.message == "first-edited"


async def test_upsert_pull_requests_persists_comments_and_review_bodies(
    session: AsyncSession,
) -> None:
    repo = await _make_repo(session)
    comments = [
        {
            "databaseId": 11,
            "body": "lgtm",
            "author": {"login": "carol"},
            "createdAt": "2026-06-01T10:30:00Z",
        },
    ]
    reviews = [
        {
            "databaseId": 22,
            "body": "nice approach",
            "author": {"login": "dave"},
            "createdAt": "2026-06-01T10:45:00Z",
        },
        # Empty-body reviews (approve / request-changes) are skipped.
        {
            "databaseId": 23,
            "body": "",
            "author": {"login": "dave"},
            "createdAt": "2026-06-01T10:46:00Z",
        },
    ]
    pr_count = await upsert_pull_requests(
        session, repo.id, [_pr_node(1, comments=comments, reviews=reviews)]
    )
    await session.commit()
    assert pr_count == 1
    rows = (await session.execute(select(PRComment))).scalars().all()
    types = sorted(c.comment_type for c in rows)
    assert types == ["issue_comment", "review_body"]


async def test_upsert_issues_persists_labels_and_comments(session: AsyncSession) -> None:
    repo = await _make_repo(session)
    count = await upsert_issues(session, repo.id, [_issue_node(1), _issue_node(2)])
    await session.commit()
    assert count == 2
    issues = (await session.execute(select(Issue).order_by(Issue.number))).scalars().all()
    assert [i.labels for i in issues] == [["bug", "p1"], ["bug", "p1"]]
    comment_count = await session.scalar(select(func.count()).select_from(IssueComment))
    assert comment_count == 2


# --- Orchestrator (mocked client) -----------------------------------------


class _FakeClient:
    """Mimics GitHubGraphQLClient; returns canned pages keyed by query header."""

    def __init__(self, pages_by_stage: dict[str, list[dict]], *, raises: Exception | None = None):
        self._pages = {k: list(v) for k, v in pages_by_stage.items()}
        self._raises = raises
        self._call_log: list[str] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, query: str, variables: dict) -> dict:
        if self._raises:
            raise self._raises
        stage = _stage_for_query(query)
        self._call_log.append(stage)
        page = self._pages[stage].pop(0)
        return _wrap(stage, page)


def _stage_for_query(query: str) -> str:
    if "history(" in query:
        return "commits"
    if "pullRequests(" in query:
        return "pull_requests"
    if "issues(" in query:
        return "issues"
    raise AssertionError(f"unrecognized query: {query[:80]}")


def _wrap(stage: str, page: dict) -> dict:
    """Embed a page (nodes + pageInfo) under the right GraphQL path."""
    rate = {"cost": 1, "limit": 5000, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"}
    if stage == "commits":
        return {
            "repository": {"defaultBranchRef": {"target": {"history": page}}},
            "rateLimit": rate,
        }
    if stage == "pull_requests":
        return {"repository": {"pullRequests": page}, "rateLimit": rate}
    return {"repository": {"issues": page}, "rateLimit": rate}


def _page(nodes: list[dict], *, end: str | None = None, has_next: bool = False) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": end}}


async def test_ingest_history_happy_path_one_page_each(
    session: AsyncSession,
) -> None:
    repo = await _make_repo(session, "tiangolo", "asyncer")
    fake = _FakeClient(
        {
            "commits": [_page([_commit_node("aaa"), _commit_node("bbb")])],
            "pull_requests": [_page([_pr_node(1), _pr_node(2)])],
            "issues": [_page([_issue_node(10)])],
        }
    )
    counts = await ingest_history(repo.id, app_db.SessionLocal, client_factory=lambda tok: fake)
    assert counts == {"commits": 2, "pull_requests": 2, "issues": 1}

    # The orchestrator wrote via a separate session_factory session; this test's
    # session has the repo cached, so refresh to see the latest state.
    await session.refresh(repo)
    assert repo.history_ingestion_status == "done"
    assert repo.history_ingestion_progress == 1.0
    assert "completed_at" in repo.history_ingestion_state

    # Each stage was hit exactly once (single page each).
    assert fake._call_log == ["commits", "pull_requests", "issues"]


async def test_ingest_history_resumes_from_state(session: AsyncSession) -> None:
    """A repo whose state already has commits done skips that stage."""
    repo = await _make_repo(session)
    repo.history_ingestion_state = {
        "window_since": "2025-01-01T00:00:00+00:00",
        "started_at": "2026-06-01T00:00:00+00:00",
        "commits": {"cursor": None, "count": 42, "done": True},
        "pull_requests": {"cursor": None, "count": 0, "done": False},
        "issues": {"cursor": None, "count": 0, "done": False},
    }
    await session.commit()

    fake = _FakeClient(
        {
            "commits": [],  # should not be touched
            "pull_requests": [_page([_pr_node(1)])],
            "issues": [_page([_issue_node(10)])],
        }
    )
    counts = await ingest_history(repo.id, app_db.SessionLocal, client_factory=lambda tok: fake)
    assert counts["commits"] == 42  # carried from state
    assert counts["pull_requests"] == 1
    assert counts["issues"] == 1
    assert fake._call_log == ["pull_requests", "issues"]


async def test_ingest_history_records_failure(session: AsyncSession) -> None:
    repo = await _make_repo(session)
    boom = RuntimeError("GraphQL exploded")
    fake = _FakeClient({"commits": [], "pull_requests": [], "issues": []}, raises=boom)

    with pytest.raises(RuntimeError):
        await ingest_history(repo.id, app_db.SessionLocal, client_factory=lambda tok: fake)

    await session.refresh(repo)
    assert repo.history_ingestion_status == "failed"
    assert "GraphQL exploded" in repo.history_ingestion_state["error"]


# --- Endpoint -------------------------------------------------------------


async def test_trigger_ingest_history_requires_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ensure no token leaks from the dev env.
    monkeypatch.setenv("GITHUB_TOKEN", "")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        async with app_db.SessionLocal() as s:
            repo = Repo(owner="o", name="r", default_branch="main")
            s.add(repo)
            await s.commit()
            repo_id = repo.id
        r = await client.post(f"/repos/{repo_id}/ingest-history")
        assert r.status_code == 400
        assert "GITHUB_TOKEN" in r.json()["detail"]
    finally:
        get_settings.cache_clear()


async def test_history_ingestion_status_reflects_done(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the full pipeline through the endpoint with a fake client."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    from app.config import get_settings
    from app.history import orchestrator as orch_mod

    get_settings.cache_clear()

    async with app_db.SessionLocal() as s:
        repo = Repo(owner="o", name="r", default_branch="main")
        s.add(repo)
        await s.commit()
        repo_id = repo.id

    fake = _FakeClient(
        {
            "commits": [_page([_commit_node("aaa")])],
            "pull_requests": [_page([_pr_node(1)])],
            "issues": [_page([_issue_node(10)])],
        }
    )

    # Patch the factory's default at the orchestrator level — the endpoint
    # spawns ingest_history without passing client_factory, so we monkey-
    # patch GitHubGraphQLClient construction to return our fake.
    monkeypatch.setattr(orch_mod, "GitHubGraphQLClient", lambda tok: fake)

    try:
        r = await client.post(f"/repos/{repo_id}/ingest-history")
        assert r.status_code == 202, r.text
        assert r.json()["history_ingestion_status"] == "in_progress"

        # BackgroundTasks run within the request lifecycle under ASGITransport.
        s_resp = await client.get(f"/repos/{repo_id}/history-ingestion-status")
        body = s_resp.json()
        assert body["history_ingestion_status"] == "done"
        assert body["commits_count"] == 1
        assert body["pull_requests_count"] == 1
        assert body["issues_count"] == 1
        assert body["error"] is None
    finally:
        get_settings.cache_clear()
