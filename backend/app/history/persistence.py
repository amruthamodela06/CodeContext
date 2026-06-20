"""Upsert GraphQL response nodes into the Slice 5 history tables.

Each `upsert_*` function takes a parsed GraphQL `nodes` list and writes rows
using Postgres ON CONFLICT DO UPDATE so re-running ingestion (e.g. after a
resume) is idempotent. Returns the number of rows written for progress tracking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commit, Issue, IssueComment, PRComment, PullRequest


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    # GitHub timestamps are RFC 3339 with `Z`; fromisoformat handles Python's
    # tz-offset form. The replace covers older Python versions / the `Z` form.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---- Commits ---------------------------------------------------------------


async def upsert_commits(session: AsyncSession, repo_id: int, nodes: list[dict[str, Any]]) -> int:
    if not nodes:
        return 0
    rows = []
    for n in nodes:
        author = n.get("author") or {}
        rows.append(
            {
                "repo_id": repo_id,
                "sha": n["oid"],
                "author_name": author.get("name"),
                "author_email": author.get("email"),
                "authored_at": _parse_iso(n.get("authoredDate")),
                "committed_at": _parse_iso(n.get("committedDate")),
                "message": n.get("message") or "",
                "parent_shas": [p["oid"] for p in (n.get("parents") or {}).get("nodes", [])],
                "files_changed_count": n.get("changedFilesIfAvailable"),
                "additions": n.get("additions"),
                "deletions": n.get("deletions"),
            }
        )
    stmt = pg_insert(Commit).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_commit_repo_sha",
        set_={
            # Only the mutable / late-arriving fields. SHA + repo_id are the
            # natural key; created_at is the row's ingest time (immutable).
            "author_name": stmt.excluded.author_name,
            "author_email": stmt.excluded.author_email,
            "authored_at": stmt.excluded.authored_at,
            "committed_at": stmt.excluded.committed_at,
            "message": stmt.excluded.message,
            "parent_shas": stmt.excluded.parent_shas,
            "files_changed_count": stmt.excluded.files_changed_count,
            "additions": stmt.excluded.additions,
            "deletions": stmt.excluded.deletions,
        },
    )
    await session.execute(stmt)
    return len(rows)


# ---- Pull requests (+ comments + review bodies) ----------------------------


async def upsert_pull_requests(
    session: AsyncSession, repo_id: int, nodes: list[dict[str, Any]]
) -> int:
    """Upsert PRs and their comments / review bodies. Returns PR count."""
    if not nodes:
        return 0

    pr_rows = []
    for n in nodes:
        author = (n.get("author") or {}).get("login")
        merge_commit = n.get("mergeCommit") or {}
        pr_rows.append(
            {
                "repo_id": repo_id,
                "number": n["number"],
                "title": n.get("title") or "",
                "body": n.get("body"),
                # GitHub returns state in uppercase (OPEN/CLOSED/MERGED); we
                # store lowercase to match the rest of the codebase.
                "state": (n.get("state") or "").lower(),
                "author": author,
                "created_at": _parse_iso(n.get("createdAt")),
                "merged_at": _parse_iso(n.get("mergedAt")),
                "closed_at": _parse_iso(n.get("closedAt")),
                "merge_commit_sha": merge_commit.get("oid"),
                "base_branch": n.get("baseRefName"),
                "head_branch": n.get("headRefName"),
                "additions": n.get("additions"),
                "deletions": n.get("deletions"),
                "files_changed_count": n.get("changedFiles"),
            }
        )

    pr_stmt = pg_insert(PullRequest).values(pr_rows)
    pr_stmt = pr_stmt.on_conflict_do_update(
        constraint="uq_pr_repo_number",
        set_={
            "title": pr_stmt.excluded.title,
            "body": pr_stmt.excluded.body,
            "state": pr_stmt.excluded.state,
            "merged_at": pr_stmt.excluded.merged_at,
            "closed_at": pr_stmt.excluded.closed_at,
            "merge_commit_sha": pr_stmt.excluded.merge_commit_sha,
            "additions": pr_stmt.excluded.additions,
            "deletions": pr_stmt.excluded.deletions,
            "files_changed_count": pr_stmt.excluded.files_changed_count,
        },
    ).returning(PullRequest.id, PullRequest.number)
    result = await session.execute(pr_stmt)
    pr_id_by_number: dict[int, int] = {row.number: row.id for row in result}

    # Comments + review bodies — flat per-PR upsert. (databaseId is the GitHub
    # numeric id; we dedupe on (pr_id, github_id).)
    comment_rows = []
    for n in nodes:
        pr_id = pr_id_by_number.get(n["number"])
        if pr_id is None:
            continue
        for c in (n.get("comments") or {}).get("nodes", []) or []:
            comment_rows.append(_comment_row(pr_id, c, "issue_comment"))
        for r in (n.get("reviews") or {}).get("nodes", []) or []:
            # Skip approve/request-changes reviews with no body text.
            if not (r.get("body") or "").strip():
                continue
            comment_rows.append(_comment_row(pr_id, r, "review_body"))

    if comment_rows:
        c_stmt = pg_insert(PRComment).values(comment_rows)
        c_stmt = c_stmt.on_conflict_do_update(
            constraint="uq_pr_comment_github",
            set_={"body": c_stmt.excluded.body},
        )
        await session.execute(c_stmt)

    return len(pr_rows)


def _comment_row(pr_id: int, node: dict[str, Any], comment_type: str) -> dict[str, Any]:
    return {
        "pr_id": pr_id,
        "github_id": node["databaseId"],
        "author": (node.get("author") or {}).get("login"),
        "body": node.get("body") or "",
        "created_at": _parse_iso(node.get("createdAt")),
        "comment_type": comment_type,
    }


# ---- Issues (+ comments) ---------------------------------------------------


async def upsert_issues(session: AsyncSession, repo_id: int, nodes: list[dict[str, Any]]) -> int:
    """Upsert issues + their comments. Returns issue count."""
    if not nodes:
        return 0

    issue_rows = []
    for n in nodes:
        author = (n.get("author") or {}).get("login")
        labels = [lab["name"] for lab in (n.get("labels") or {}).get("nodes", [])]
        issue_rows.append(
            {
                "repo_id": repo_id,
                "number": n["number"],
                "title": n.get("title") or "",
                "body": n.get("body"),
                "state": (n.get("state") or "").lower(),
                "author": author,
                "created_at": _parse_iso(n.get("createdAt")),
                "closed_at": _parse_iso(n.get("closedAt")),
                "labels": labels,
                # closing_pr_number left null — Slice 5c fills it during edge
                # construction from PR-body parsing.
            }
        )

    i_stmt = pg_insert(Issue).values(issue_rows)
    i_stmt = i_stmt.on_conflict_do_update(
        constraint="uq_issue_repo_number",
        set_={
            "title": i_stmt.excluded.title,
            "body": i_stmt.excluded.body,
            "state": i_stmt.excluded.state,
            "closed_at": i_stmt.excluded.closed_at,
            "labels": i_stmt.excluded.labels,
        },
    ).returning(Issue.id, Issue.number)
    result = await session.execute(i_stmt)
    issue_id_by_number: dict[int, int] = {row.number: row.id for row in result}

    comment_rows = []
    for n in nodes:
        issue_id = issue_id_by_number.get(n["number"])
        if issue_id is None:
            continue
        for c in (n.get("comments") or {}).get("nodes", []) or []:
            comment_rows.append(
                {
                    "issue_id": issue_id,
                    "github_id": c["databaseId"],
                    "author": (c.get("author") or {}).get("login"),
                    "body": c.get("body") or "",
                    "created_at": _parse_iso(c.get("createdAt")),
                }
            )

    if comment_rows:
        c_stmt = pg_insert(IssueComment).values(comment_rows)
        c_stmt = c_stmt.on_conflict_do_update(
            constraint="uq_issue_comment_github",
            set_={"body": c_stmt.excluded.body},
        )
        await session.execute(c_stmt)

    return len(issue_rows)
