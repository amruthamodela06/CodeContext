"""History-ingestion background job. See ADR 0011.

Three sequential stages (commits, pull requests, issues). Cursor + count for
each stage are persisted to `repo.history_ingestion_state` after every page
so a daemon restart or rate-limit pause resumes from the next page rather
than starting over.

Failure model: if a stage hits an unrecoverable error (auth, malformed
response), mark the repo `failed` with the error in `state['error']`. If a
stage hits a transient error the client already retried, we still surface it
as `failed`. The lifespan orphan-recovery sweeps `in_progress` -> `failed`
on restart so a crashed worker doesn't leave the repo stuck mid-stream.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.history.client import GitHubGraphQLClient
from app.history.persistence import (
    upsert_commits,
    upsert_issues,
    upsert_pull_requests,
)
from app.history.queries import COMMITS_QUERY, ISSUES_QUERY, PULL_REQUESTS_QUERY
from app.models import Repo

log = logging.getLogger(__name__)

# Stages of progress. Each contributes equally (1/3). Within a stage progress
# is binary (started but not done = current_stage/3; done = (current+1)/3).
_STAGES = ("commits", "pull_requests", "issues")


@dataclass(frozen=True)
class _StageSpec:
    name: str
    query: str
    # `path` walks the GraphQL `data` dict to the connection node. Lifts the
    # quirk that commits live at data.repository.defaultBranchRef.target.history
    # while PRs/issues are at data.repository.pullRequests / .issues.
    path: tuple[str, ...]
    upsert: Callable[[AsyncSession, int, list[dict[str, Any]]], Awaitable[int]]
    needs_since: bool
    # PRs / issues are ordered UPDATED_AT DESC; we stop paging once a page's
    # newest item falls before the window cutoff. Field name to compare; None
    # for commits (filtered by GraphQL's `since` argument directly).
    cutoff_field: str | None


_STAGE_SPECS: dict[str, _StageSpec] = {
    "commits": _StageSpec(
        name="commits",
        query=COMMITS_QUERY,
        path=("repository", "defaultBranchRef", "target", "history"),
        upsert=upsert_commits,
        needs_since=True,
        cutoff_field=None,
    ),
    "pull_requests": _StageSpec(
        name="pull_requests",
        query=PULL_REQUESTS_QUERY,
        path=("repository", "pullRequests"),
        upsert=upsert_pull_requests,
        needs_since=False,
        cutoff_field="updatedAt",
    ),
    "issues": _StageSpec(
        name="issues",
        query=ISSUES_QUERY,
        path=("repository", "issues"),
        upsert=upsert_issues,
        needs_since=False,
        cutoff_field="updatedAt",
    ),
}


async def ingest_history(
    repo_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_factory: Callable[[str], GitHubGraphQLClient] | None = None,
) -> dict[str, int]:
    """Run history ingestion for one repo. Returns per-stage counts.

    `client_factory` is injected for tests; production uses the default
    GitHubGraphQLClient backed by httpx.
    """
    settings = get_settings()
    factory = client_factory or (lambda tok: GitHubGraphQLClient(tok))

    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:
            log.warning("ingest_history: repo %d not found", repo_id)
            return {}

        owner, name = repo.owner, repo.name
        state = _ensure_state(repo, settings.history_ingestion_months)
        repo.history_ingestion_status = "in_progress"
        await _save_state(session, repo, state)

    counts: dict[str, int] = {s: 0 for s in _STAGES}

    try:
        async with factory(settings.github_token) as client:
            for stage_name in _STAGES:
                count = await _run_stage(
                    client, session_factory, repo_id, owner, name, stage_name, state
                )
                counts[stage_name] = count
    except Exception as exc:
        log.exception("ingest_history: repo %d failed in stage", repo_id)
        async with session_factory() as session:
            repo = await session.get(Repo, repo_id)
            if repo is not None:
                state["error"] = f"{type(exc).__name__}: {exc}"[:500]
                repo.history_ingestion_status = "failed"
                repo.history_ingestion_progress = _progress_for_state(state)
                await _save_state(session, repo, state)
        raise

    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is not None:
            repo.history_ingestion_status = "done"
            repo.history_ingestion_progress = 1.0
            state["completed_at"] = datetime.now(UTC).isoformat()
            await _save_state(session, repo, state)

    return counts


async def _run_stage(
    client: GitHubGraphQLClient,
    session_factory: async_sessionmaker[AsyncSession],
    repo_id: int,
    owner: str,
    name: str,
    stage_name: str,
    state: dict,
) -> int:
    spec = _STAGE_SPECS[stage_name]
    stage_state = state.setdefault(stage_name, {"cursor": None, "count": 0, "done": False})
    if stage_state.get("done"):
        return int(stage_state.get("count", 0))

    cutoff = datetime.fromisoformat(state["window_since"])
    count = int(stage_state.get("count", 0))

    while True:
        variables: dict[str, Any] = {
            "owner": owner,
            "name": name,
            "cursor": stage_state.get("cursor"),
        }
        if spec.needs_since:
            variables["since"] = state["window_since"]

        data = await client.execute(spec.query, variables)
        connection = _walk(data, spec.path)
        if connection is None:
            # Repo without a defaultBranchRef, or stage path returned null —
            # nothing to do; mark stage done so we don't loop.
            stage_state["done"] = True
            async with session_factory() as session:
                repo = await session.get(Repo, repo_id)
                if repo is not None:
                    repo.history_ingestion_progress = _progress_for_state(state)
                    await _save_state(session, repo, state)
            return count

        nodes = connection.get("nodes") or []
        page_info = connection.get("pageInfo") or {}

        # Client-side window cutoff for PRs / issues — stop paging once
        # the entire page is older than window_since (the connection is
        # sorted UPDATED_AT DESC, so once one page is fully past the
        # cutoff, every later page is too).
        if spec.cutoff_field:
            nodes = _filter_by_cutoff(nodes, spec.cutoff_field, cutoff)
            all_past_cutoff = nodes == [] and connection.get("nodes")
        else:
            all_past_cutoff = False

        async with session_factory() as session:
            written = await spec.upsert(session, repo_id, nodes)
            count += written
            stage_state["count"] = count
            stage_state["cursor"] = page_info.get("endCursor")

            has_next = page_info.get("hasNextPage", False) and not all_past_cutoff
            if not has_next:
                stage_state["done"] = True
                await session.commit()
                repo = await session.get(Repo, repo_id)
                if repo is not None:
                    repo.history_ingestion_progress = _progress_for_state(state)
                    await _save_state(session, repo, state)
                break

            repo = await session.get(Repo, repo_id)
            if repo is not None:
                repo.history_ingestion_progress = _progress_for_state(state)
                await _save_state(session, repo, state)

    return count


# ---- State helpers ---------------------------------------------------------


def _ensure_state(repo: Repo, months: int) -> dict:
    """Initialize state on first run, or carry the existing one on resume."""
    state = dict(repo.history_ingestion_state or {})
    state.setdefault(
        "window_since",
        (datetime.now(UTC) - timedelta(days=30 * months)).isoformat(),
    )
    state.setdefault("started_at", datetime.now(UTC).isoformat())
    state.pop("error", None)  # clear any prior failure
    for s in _STAGES:
        state.setdefault(s, {"cursor": None, "count": 0, "done": False})
    return state


def _progress_for_state(state: dict) -> float:
    """0.0 - 1.0 progress. Each completed stage contributes 1/3."""
    done = sum(1 for s in _STAGES if state.get(s, {}).get("done"))
    return done / len(_STAGES)


async def _save_state(session: AsyncSession, repo: Repo, state: dict) -> None:
    repo.history_ingestion_state = state
    # JSONB column changes via in-place mutation don't auto-flag dirty.
    flag_modified(repo, "history_ingestion_state")
    await session.commit()


def _walk(payload: dict, path: tuple[str, ...]) -> dict | None:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def _filter_by_cutoff(nodes: list[dict], field: str, cutoff: datetime) -> list[dict]:
    out = []
    for n in nodes:
        raw = n.get(field)
        if not raw:
            continue
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts < cutoff:
            continue
        out.append(n)
    return out


async def select_history_counts(session: AsyncSession, repo_id: int) -> dict[str, int]:
    """Live counts from the DB for the status endpoint."""
    from sqlalchemy import func as sql_func

    from app.models import Commit, Issue, PullRequest

    async def _count(model) -> int:
        return (
            await session.scalar(select(sql_func.count(model.id)).where(model.repo_id == repo_id))
            or 0
        )

    return {
        "commits": await _count(Commit),
        "pull_requests": await _count(PullRequest),
        "issues": await _count(Issue),
    }
