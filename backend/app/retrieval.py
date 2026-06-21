"""Top-level retrieval orchestrator. See ADR 0012.

``retrieve_entities`` is the single entry point Slice 5g's POST /query
calls. It routes on category:

- out_of_scope          -> empty context (no retrieval; /query short-circuits).
- lookup / architectural / impact -> flat vector retrieval over chunks.
- historical_why        -> flat chunks + graph expansion (chunk -> commit ->
                           pr within depth=2 / breadth=10) + embedding rerank
                           of the expanded candidate set; resulting commits/
                           PRs join the CitationContext alongside the chunks.

Returns a populated CitationContext (chunks + maybe commits/PRs/issues),
ready for the prompt builder + the SSE stream + the final citation
validator.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.citations.context import (
    CitationContext,
    CitedChunk,
    CitedCommit,
    CitedIssue,
    CitedPR,
)
from app.classifier import Category
from app.embeddings import get_embedder
from app.graph.multihop import rerank_by_embedding, traverse_outbound
from app.models import Commit, EntityEdge, Issue, PullRequest

log = logging.getLogger(__name__)


async def retrieve_entities(
    session: AsyncSession,
    repo_id: int,
    query: str,
    *,
    top_k: int,
    category: Category,
    retrieve_chunks_fn,  # the existing api.retrieve_chunks helper; injected
) -> tuple[CitationContext, dict]:
    """Returns (context, debug_trace).

    The trace exposes routing decisions for the /query response's debug
    panel: classified category, seed chunk IDs, expansion candidate count,
    reranked entity count.
    """
    trace: dict = {"category": category}

    if category == "out_of_scope":
        return CitationContext(), trace

    # Stage 1 -- flat chunk retrieval (always).
    flat_results = await retrieve_chunks_fn(session, repo_id, query, top_k)
    chunks = _materialize_chunks(flat_results)
    trace["seed_chunk_ids"] = [c.chunk_id for c in chunks]

    if category != "historical_why" or not chunks:
        return CitationContext(chunks=chunks), trace

    # Stage 2 -- graph expansion (chunk -> commit -> pr at depth 2).
    seed_ids = [c.chunk_id for c in chunks]
    expanded = await traverse_outbound(session, repo_id, seed_ids)
    trace["expansion_candidates"] = len(expanded)
    if not expanded:
        return CitationContext(chunks=chunks), trace

    # Stage 3 -- embedding rerank the expanded set against the query.
    embedder = get_embedder()
    query_vec = await asyncio.to_thread(embedder.embed_one, query)
    candidate_pairs = [(t, i) for t, i, _ in expanded]
    reranked = await rerank_by_embedding(session, repo_id, candidate_pairs, query_vec, top_k=top_k)
    trace["reranked_count"] = len(reranked)
    if not reranked:
        return CitationContext(chunks=chunks), trace

    # Stage 4 -- hydrate full entity rows + render relationships.
    commits, prs, issues = await _hydrate_entities(session, repo_id, reranked)
    relations = await _build_relations(session, repo_id, chunks, commits, prs, issues)

    ctx = CitationContext(
        chunks=chunks,
        commits=commits,
        prs=prs,
        issues=issues,
        relations=relations,
    )
    return ctx, trace


# ---- Helpers -------------------------------------------------------------


def _materialize_chunks(results: Iterable) -> list[CitedChunk]:
    """Slice-4-shaped SearchResult rows -> CitedChunk(c1..cN)."""
    return [
        CitedChunk(
            display_id=f"c{i + 1}",
            chunk_id=r.chunk_id,
            file_path=r.file_path,
            start_line=r.start_line,
            end_line=r.end_line,
            language=r.language,
            chunk_type=r.chunk_type,
            name=r.name,
            content=r.content,
            similarity=r.similarity,
        )
        for i, r in enumerate(results)
    ]


async def _hydrate_entities(
    session: AsyncSession,
    repo_id: int,
    reranked: list[tuple[str, int, float]],
) -> tuple[list[CitedCommit], list[CitedPR], list[CitedIssue]]:
    """Fetch the underlying rows for the reranked (type, id) pairs and
    assemble per-type CitedX lists in similarity-descending order.
    Display IDs assigned as m1/m2..., p1/p2..., i1/i2... in rerank order.
    """
    # Pull the per-type ids in rerank order so display IDs honor relevance.
    commit_ids = [eid for et, eid, _ in reranked if et == "commit"]
    pr_ids = [eid for et, eid, _ in reranked if et == "pr"]
    issue_ids = [eid for et, eid, _ in reranked if et == "issue"]
    sims: dict[tuple[str, int], float] = {(et, eid): s for et, eid, s in reranked}

    commits: list[CitedCommit] = []
    if commit_ids:
        rows = (
            (await session.execute(select(Commit).where(Commit.id.in_(commit_ids)))).scalars().all()
        )
        by_id = {r.id: r for r in rows}
        for i, cid in enumerate(commit_ids):
            r = by_id.get(cid)
            if r is None:
                continue
            commits.append(
                CitedCommit(
                    display_id=f"m{i + 1}",
                    commit_id=r.id,
                    sha=r.sha,
                    author_name=r.author_name,
                    authored_at=r.authored_at,
                    message=r.message,
                    similarity=sims[("commit", cid)],
                )
            )

    prs: list[CitedPR] = []
    if pr_ids:
        rows = (
            (await session.execute(select(PullRequest).where(PullRequest.id.in_(pr_ids))))
            .scalars()
            .all()
        )
        by_id = {r.id: r for r in rows}
        for i, pid in enumerate(pr_ids):
            r = by_id.get(pid)
            if r is None:
                continue
            prs.append(
                CitedPR(
                    display_id=f"p{i + 1}",
                    pr_id=r.id,
                    number=r.number,
                    title=r.title,
                    body=r.body,
                    state=r.state,
                    merged_at=r.merged_at,
                    similarity=sims[("pr", pid)],
                )
            )

    issues: list[CitedIssue] = []
    if issue_ids:
        rows = (await session.execute(select(Issue).where(Issue.id.in_(issue_ids)))).scalars().all()
        by_id = {r.id: r for r in rows}
        for i, iid in enumerate(issue_ids):
            r = by_id.get(iid)
            if r is None:
                continue
            issues.append(
                CitedIssue(
                    display_id=f"i{i + 1}",
                    issue_id=r.id,
                    number=r.number,
                    title=r.title,
                    body=r.body,
                    state=r.state,
                    closed_at=r.closed_at,
                    similarity=sims[("issue", iid)],
                )
            )

    return commits, prs, issues


async def _build_relations(
    session: AsyncSession,
    repo_id: int,
    chunks: list[CitedChunk],
    commits: list[CitedCommit],
    prs: list[CitedPR],
    issues: list[CitedIssue],
) -> dict[tuple[str, str], str]:
    """Pre-compute relationship phrases for excerpt headers.

    Walks entity_edge for the entities actually in the context and emits
    short phrases like "introduced [c1]" / "contains [m2]" / "closed by
    [p3]" that the renderer drops into each header. Lets the LLM trace
    chains without us hand-writing prose in every excerpt.
    """
    if not commits and not prs and not issues:
        return {}

    # Build (entity_type, entity_id) -> display_id lookups.
    chunk_disp = {c.chunk_id: c.display_id for c in chunks}
    commit_disp = {m.commit_id: m.display_id for m in commits}
    pr_disp = {p.pr_id: p.display_id for p in prs}
    issue_disp = {i.issue_id: i.display_id for i in issues}

    # Fetch every edge that has either endpoint in the context.
    relevant_ids = (
        [("chunk", cid) for cid in chunk_disp]
        + [("commit", cid) for cid in commit_disp]
        + [("pr", pid) for pid in pr_disp]
        + [("issue", iid) for iid in issue_disp]
    )
    if not relevant_ids:
        return {}

    from sqlalchemy import or_, tuple_

    rows = (
        await session.execute(
            select(
                EntityEdge.source_type,
                EntityEdge.source_id,
                EntityEdge.target_type,
                EntityEdge.target_id,
                EntityEdge.edge_type,
            ).where(
                EntityEdge.repo_id == repo_id,
                or_(
                    tuple_(EntityEdge.source_type, EntityEdge.source_id).in_(relevant_ids),
                    tuple_(EntityEdge.target_type, EntityEdge.target_id).in_(relevant_ids),
                ),
            )
        )
    ).all()

    disp_for = {
        "chunk": chunk_disp,
        "commit": commit_disp,
        "pr": pr_disp,
        "issue": issue_disp,
    }
    relations: dict[tuple[str, str], dict[str, list[str]]] = {}

    def _disp(et: str, eid: int) -> str | None:
        return disp_for.get(et, {}).get(eid)

    # Map edge_type -> (phrase template applied to the OTHER side's display_id).
    # The relationship phrase lives on the *source* row's header by default;
    # for closed_by we flip it so it shows on the issue.
    for src_t, src_i, tgt_t, tgt_i, edge_type in rows:
        src_disp = _disp(src_t, src_i)
        tgt_disp = _disp(tgt_t, tgt_i)
        if not src_disp or not tgt_disp:
            continue
        if edge_type == "introduced_by":
            # commit "introduced [c1]" -- attach to commit header.
            _add(relations, (tgt_t, tgt_disp), f"introduced [{src_t}:{src_disp}]")
        elif edge_type == "part_of":
            # pr "contains [m2]" -- attach to pr header.
            _add(relations, (tgt_t, tgt_disp), f"contains [{src_t}:{src_disp}]")
        elif edge_type == "references_issue":
            _add(relations, (src_t, src_disp), f"references [{tgt_t}:{tgt_disp}]")
        elif edge_type == "closed_by":
            # issue closed by pr -- attach to issue header.
            _add(relations, (src_t, src_disp), f"closed by [{tgt_t}:{tgt_disp}]")

    return {key: ", ".join(phrases) for key, phrases in _flatten(relations).items()}


def _add(
    bag: dict[tuple[str, str], dict[str, list[str]]],
    key: tuple[str, str],
    phrase: str,
) -> None:
    bag.setdefault(key, {"phrases": []})["phrases"].append(phrase)


def _flatten(
    bag: dict[tuple[str, str], dict[str, list[str]]],
) -> dict[tuple[str, str], list[str]]:
    return {k: v["phrases"] for k, v in bag.items()}


# Avoid pyflakes warning -- datetime import kept for forward-compat hooks.
_ = datetime
