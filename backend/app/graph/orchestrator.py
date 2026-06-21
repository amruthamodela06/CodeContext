"""Graph-build background job (Slice 5c). See ADR 0012.

Re-clones the repo (blame needs the working tree) and walks three stages:

1. ``chunk -[introduced_by]-> commit`` -- per-file git blame, attribute each
   chunk's start_line to a commit. Unknown SHAs (older than the GraphQL
   12-month window) are stub-inserted from local git data so the edge
   always has a valid target.
2. ``commit -[part_of]-> pr`` -- match each PR's ``merge_commit_sha`` to a
   commit row.
3. ``pr -[references_issue]-> issue`` + ``issue -[closed_by]-> pr`` -- parse
   PR title/body for "fixes #N" style references. The closed-by inverse
   only fires when the PR is merged. ``Issue.closing_pr_number`` is filled
   alongside.

Each stage commits its rows as a batch and updates ``repo.graph_state`` /
``graph_progress`` so a status poll sees forward motion. Stages run in
order; a failure in any stage marks the repo failed (graph build is not
itself resumable in v1 -- it re-runs from scratch in a few seconds for
asyncer-sized repos; large-repo resumability is a Slice 7+ concern if the
profile demands it).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.graph.blame import blame_file, fetch_commit_stub
from app.graph.pr_parsing import extract_closing_issue_numbers
from app.ingest import clone_repo
from app.models import (
    CodeChunk,
    Commit,
    EntityEdge,
    Issue,
    PullRequest,
    Repo,
)

log = logging.getLogger(__name__)


# ---- Entry point ----------------------------------------------------------


async def build_graph(
    repo_id: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Run all three edge-construction stages. Returns per-edge-type counts."""
    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:
            log.warning("build_graph: repo %d not found", repo_id)
            return {}
        owner, name = repo.owner, repo.name
        state: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat()}
        repo.graph_status = "in_progress"
        repo.graph_progress = 0.0
        repo.graph_state = state
        flag_modified(repo, "graph_state")
        await session.commit()

    counts = {
        "introduced_by": 0,
        "part_of": 0,
        "references_issue": 0,
        "closed_by": 0,
    }

    try:
        clone_url = f"https://github.com/{owner}/{name}.git"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            dest = Path(tmp) / "repo"
            # Full-depth clone — blame needs the history to walk past the
            # current state of each line. (ingest's shallow --depth 1 is fine
            # for /ingest because it only walks the working tree, not history.)
            _full_clone(clone_url, dest)

            async with session_factory() as session:
                counts["introduced_by"] = await _stage_introduced_by(session, repo_id, dest)
                await _bump_progress(session, repo_id, state, 1 / 3)

                counts["part_of"] = await _stage_part_of(session, repo_id)
                await _bump_progress(session, repo_id, state, 2 / 3)

                cited, closed = await _stage_pr_issue_links(session, repo_id)
                counts["references_issue"] = cited
                counts["closed_by"] = closed
                await _bump_progress(session, repo_id, state, 1.0)
    except Exception as exc:
        log.exception("build_graph: repo %d failed", repo_id)
        async with session_factory() as session:
            repo = await session.get(Repo, repo_id)
            if repo is not None:
                state["error"] = f"{type(exc).__name__}: {exc}"[:500]
                state["counts"] = counts
                repo.graph_status = "failed"
                repo.graph_state = state
                flag_modified(repo, "graph_state")
                await session.commit()
        raise

    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is not None:
            state["completed_at"] = datetime.now(UTC).isoformat()
            state["counts"] = counts
            repo.graph_status = "done"
            repo.graph_progress = 1.0
            repo.graph_state = state
            flag_modified(repo, "graph_state")
            await session.commit()
    return counts


def _full_clone(clone_url: str, dest: Path) -> None:
    # Reuse ingest.clone_repo for default-branch / SHA capture, but it does a
    # shallow --depth 1 clone. For blame we need full history; unshallow.
    clone_repo(clone_url, dest)
    subprocess.run(
        ["git", "-C", str(dest), "fetch", "--unshallow"],
        check=False,  # already-complete clones fail this; ignore that case
        capture_output=True,
    )


async def _bump_progress(session: AsyncSession, repo_id: int, state: dict, progress: float) -> None:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        return
    repo.graph_progress = progress
    repo.graph_state = state
    flag_modified(repo, "graph_state")
    await session.commit()


# ---- Stage 1: chunk -[introduced_by]-> commit -----------------------------


async def _stage_introduced_by(session: AsyncSession, repo_id: int, repo_root: Path) -> int:
    """Per-file blame, look up each chunk's start_line, write edges.

    Stubs in unknown commits from local git data so the edge always has a
    valid target. Re-uses an in-memory sha->id map per repo to avoid one
    DB round-trip per chunk.
    """
    # Pull chunks grouped by file. selectinload pulls File.path in one query.
    chunk_rows = (
        (
            await session.execute(
                select(CodeChunk)
                .where(CodeChunk.repo_id == repo_id)
                .options(selectinload(CodeChunk.file))
            )
        )
        .scalars()
        .all()
    )
    if not chunk_rows:
        return 0

    by_file: dict[str, list[CodeChunk]] = {}
    for c in chunk_rows:
        by_file.setdefault(c.file.path, []).append(c)

    # Existing commits in this repo, sha -> id.
    sha_to_id: dict[str, int] = dict(
        (
            await session.execute(select(Commit.sha, Commit.id).where(Commit.repo_id == repo_id))
        ).all()
    )

    edges_inserted = 0
    skipped_files = 0
    skipped_chunks = 0

    for file_path, chunks in by_file.items():
        line_map = blame_file(repo_root, file_path)
        if line_map is None:
            skipped_files += 1
            continue

        # Bucket chunks of this file by needed commit_id; stub-insert any
        # SHAs we've never seen.
        chunk_edges: list[tuple[int, int, int]] = []  # (chunk_id, commit_id, line)
        for chunk in chunks:
            sha = line_map.get(chunk.start_line)
            if not sha:
                skipped_chunks += 1
                continue
            commit_id = sha_to_id.get(sha)
            if commit_id is None:
                stub = fetch_commit_stub(repo_root, sha)
                if stub is None:
                    skipped_chunks += 1
                    continue
                commit_id = await _upsert_stub_commit(session, repo_id, stub)
                sha_to_id[sha] = commit_id
            chunk_edges.append((chunk.id, commit_id, chunk.start_line))

        if chunk_edges:
            edges_inserted += await _bulk_insert_edges(
                session,
                repo_id,
                edge_type="introduced_by",
                source_type="chunk",
                target_type="commit",
                pairs=[(c, t) for c, t, _ in chunk_edges],
                metadata_per_pair=[{"blame_line": line} for _, _, line in chunk_edges],
            )

    await session.commit()
    if skipped_files or skipped_chunks:
        log.info(
            "build_graph[introduced_by]: skipped %d files / %d chunks",
            skipped_files,
            skipped_chunks,
        )
    return edges_inserted


async def _upsert_stub_commit(session: AsyncSession, repo_id: int, stub: dict) -> int:
    """Insert a minimal commit row (returning id) for an unknown SHA.

    ON CONFLICT DO NOTHING because a GraphQL-fetched commit may race in;
    we never want to overwrite a real row with a stub.
    """
    payload = {
        "repo_id": repo_id,
        "sha": stub["sha"],
        "author_name": stub.get("author_name"),
        "author_email": stub.get("author_email"),
        "authored_at": _parse_or_none(stub.get("authored_at")),
        "committed_at": _parse_or_none(stub.get("committed_at")),
        "message": stub.get("message") or "",
        "parent_shas": [],
    }
    stmt = pg_insert(Commit).values([payload])
    stmt = stmt.on_conflict_do_nothing(constraint="uq_commit_repo_sha")
    await session.execute(stmt)
    # Whether we inserted or hit the conflict, the row exists now -- look it up.
    commit_id = await session.scalar(
        select(Commit.id).where(Commit.repo_id == repo_id, Commit.sha == stub["sha"])
    )
    return int(commit_id)


def _parse_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- Stage 2: commit -[part_of]-> pr --------------------------------------


async def _stage_part_of(session: AsyncSession, repo_id: int) -> int:
    """One edge per (PR.merge_commit_sha matches a known commit) pair."""
    rows = (
        await session.execute(
            select(PullRequest.id, PullRequest.merge_commit_sha)
            .where(PullRequest.repo_id == repo_id)
            .where(PullRequest.merge_commit_sha.is_not(None))
        )
    ).all()
    if not rows:
        return 0

    sha_to_pr: dict[str, int] = {sha: pr_id for pr_id, sha in rows}
    # Find the commit IDs for these SHAs in one query.
    commit_rows = (
        await session.execute(
            select(Commit.id, Commit.sha).where(
                Commit.repo_id == repo_id,
                Commit.sha.in_(list(sha_to_pr.keys())),
            )
        )
    ).all()

    pairs = [(commit_id, sha_to_pr[sha]) for commit_id, sha in commit_rows if sha in sha_to_pr]
    if not pairs:
        return 0

    count = await _bulk_insert_edges(
        session,
        repo_id,
        edge_type="part_of",
        source_type="commit",
        target_type="pr",
        pairs=pairs,
    )
    await session.commit()
    return count


# ---- Stage 3: pr -[references_issue]-> issue + issue -[closed_by]-> pr ----


async def _stage_pr_issue_links(session: AsyncSession, repo_id: int) -> tuple[int, int]:
    """Parse PR titles/bodies for ``fixes #N`` style refs; write both edges
    (references_issue + closed_by-on-merge) and populate
    ``Issue.closing_pr_number``.
    """
    prs = (
        await session.execute(
            select(
                PullRequest.id,
                PullRequest.number,
                PullRequest.title,
                PullRequest.body,
                PullRequest.state,
            ).where(PullRequest.repo_id == repo_id)
        )
    ).all()
    if not prs:
        return 0, 0

    # Issue number -> id map for this repo.
    issue_rows = (
        await session.execute(select(Issue.id, Issue.number).where(Issue.repo_id == repo_id))
    ).all()
    issue_id_by_num: dict[int, int] = {num: iid for iid, num in issue_rows}

    references_pairs: list[tuple[int, int]] = []  # (pr_id, issue_id)
    closed_pairs: list[tuple[int, int]] = []  # (issue_id, pr_id)
    pr_number_for_issue: dict[int, int] = {}  # issue_id -> pr_number (for closing_pr_number)

    for pr_id, pr_number, title, body, state in prs:
        for issue_num in extract_closing_issue_numbers(title, body):
            issue_id = issue_id_by_num.get(issue_num)
            if issue_id is None:
                continue
            references_pairs.append((pr_id, issue_id))
            if state == "merged":
                closed_pairs.append((issue_id, pr_id))
                pr_number_for_issue[issue_id] = pr_number

    ref_count = (
        await _bulk_insert_edges(
            session,
            repo_id,
            edge_type="references_issue",
            source_type="pr",
            target_type="issue",
            pairs=references_pairs,
        )
        if references_pairs
        else 0
    )
    closed_count = (
        await _bulk_insert_edges(
            session,
            repo_id,
            edge_type="closed_by",
            source_type="issue",
            target_type="pr",
            pairs=closed_pairs,
        )
        if closed_pairs
        else 0
    )

    # Fill Issue.closing_pr_number for issues actually closed by a merged PR.
    for issue_id, pr_number in pr_number_for_issue.items():
        await session.execute(
            Issue.__table__.update().where(Issue.id == issue_id).values(closing_pr_number=pr_number)
        )

    await session.commit()
    return ref_count, closed_count


# ---- Edge upsert helper ---------------------------------------------------


async def _bulk_insert_edges(
    session: AsyncSession,
    repo_id: int,
    *,
    edge_type: str,
    source_type: str,
    target_type: str,
    pairs: Iterable[tuple[int, int]],
    metadata_per_pair: list[dict] | None = None,
) -> int:
    pair_list = list(pairs)
    if not pair_list:
        return 0
    rows = []
    for i, (src_id, tgt_id) in enumerate(pair_list):
        rows.append(
            {
                "repo_id": repo_id,
                "source_type": source_type,
                "source_id": src_id,
                "target_type": target_type,
                "target_id": tgt_id,
                "edge_type": edge_type,
                "edge_metadata": (metadata_per_pair[i] if metadata_per_pair else {}),
            }
        )
    stmt = pg_insert(EntityEdge).values(rows)
    # Idempotent on re-run: UNIQUE on (repo, src, tgt, type) skips dupes.
    stmt = stmt.on_conflict_do_nothing(constraint="uq_entity_edge_distinct")
    await session.execute(stmt)
    return len(rows)


# ---- Status endpoint helper ----------------------------------------------


async def select_edge_counts_by_type(session: AsyncSession, repo_id: int) -> dict[str, int]:
    """Live counts per edge_type for the status endpoint."""
    from sqlalchemy import func as sql_func

    rows = (
        await session.execute(
            select(EntityEdge.edge_type, sql_func.count(EntityEdge.id))
            .where(EntityEdge.repo_id == repo_id)
            .group_by(EntityEdge.edge_type)
        )
    ).all()
    out = {"introduced_by": 0, "part_of": 0, "references_issue": 0, "closed_by": 0}
    for et, n in rows:
        out[et] = n
    return out
