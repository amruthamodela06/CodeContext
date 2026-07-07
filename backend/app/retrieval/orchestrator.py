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
import time
from datetime import datetime
from typing import TYPE_CHECKING

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
from app.models import CodeChunk, Commit, EntityEdge, File, Issue, PullRequest
from app.retrieval.protocol import RetrievalFilters, RetrievalResult

if TYPE_CHECKING:
    from app.retrieval.protocol import Retriever

log = logging.getLogger(__name__)


async def retrieve_entities(
    session: AsyncSession,
    repo_id: int,
    query: str,
    *,
    top_k: int,
    category: Category,
    retriever: Retriever | None = None,
) -> tuple[CitationContext, dict]:
    """Returns (context, debug_trace).

    Stage 1 routes chunk retrieval through the configured
    ``Retriever`` (Slice 6 -- vector / bm25 / hybrid / hybrid_rerank).
    Tests can inject a stub retriever; production uses ``get_retriever()``.

    The trace exposes routing decisions for the /query response's debug
    panel: retrieval mode, classified category, seed chunk IDs,
    expansion candidate count, reranked entity count.
    """
    from app.retrieval import get_retriever

    trace: dict = {"category": category}
    stage_timings: dict[str, float] = {}
    trace["stage_timings_ms"] = stage_timings

    if category == "out_of_scope":
        return CitationContext(), trace

    r = retriever if retriever is not None else get_retriever()
    trace["retrieval_mode"] = r.name

    # Stage 1 -- flat chunk retrieval via the configured retriever. Filter to
    # chunks only; history entities come from Slice 5's multi-hop expansion
    # below, not from flat stage-1.
    t0 = time.perf_counter()
    flat_results = await r.retrieve(
        session,
        repo_id,
        query,
        top_k,
        filters=RetrievalFilters(entity_types={"chunk"}),
    )
    chunks = await _hydrate_chunks(session, flat_results)
    stage_timings["retrieve"] = round((time.perf_counter() - t0) * 1000, 1)
    trace["seed_chunk_ids"] = [c.chunk_id for c in chunks]

    if category != "historical_why" or not chunks:
        return CitationContext(chunks=chunks), trace

    # Stage 2 -- graph expansion (chunk -> commit -> pr at depth 2).
    t0 = time.perf_counter()
    seed_ids = [c.chunk_id for c in chunks]
    expanded = await traverse_outbound(session, repo_id, seed_ids)
    stage_timings["expand"] = round((time.perf_counter() - t0) * 1000, 1)
    trace["expansion_candidates"] = len(expanded)
    if not expanded:
        return CitationContext(chunks=chunks), trace

    # Stage 3 -- embedding rerank the expanded set against the query.
    t0 = time.perf_counter()
    embedder = get_embedder()
    query_vec = await asyncio.to_thread(embedder.embed_one, query)
    candidate_pairs = [(t, i) for t, i, _ in expanded]
    reranked = await rerank_by_embedding(session, repo_id, candidate_pairs, query_vec, top_k=top_k)
    stage_timings["multihop_rerank"] = round((time.perf_counter() - t0) * 1000, 1)
    trace["reranked_count"] = len(reranked)
    if not reranked:
        return CitationContext(chunks=chunks), trace

    # Stage 4 -- hydrate full entity rows + render relationships.
    t0 = time.perf_counter()
    commits, prs, issues = await _hydrate_entities(session, repo_id, reranked)
    relations = await _build_relations(session, repo_id, chunks, commits, prs, issues)
    stage_timings["hydrate"] = round((time.perf_counter() - t0) * 1000, 1)

    ctx = CitationContext(
        chunks=chunks,
        commits=commits,
        prs=prs,
        issues=issues,
        relations=relations,
    )
    return ctx, trace


# ---- Helpers -------------------------------------------------------------


async def _hydrate_chunks(
    session: AsyncSession, results: list[RetrievalResult]
) -> list[CitedChunk]:
    """RetrievalResult pointers -> CitedChunk rows (with content + file
    path). Preserves the retriever's rank ordering; assigns display IDs
    c1..cN over the surviving chunks.

    Non-chunk results are ignored -- callers filter at the Retriever
    layer, this is defensive against future callers that pass mixed
    types.
    """
    chunk_ids = [r.entity_id for r in results if r.entity_type == "chunk"]
    if not chunk_ids:
        return []
    rows = (
        await session.execute(
            select(CodeChunk, File.path)
            .join(File, File.id == CodeChunk.file_id)
            .where(CodeChunk.id.in_(chunk_ids))
        )
    ).all()
    by_id = {chunk.id: (chunk, path) for chunk, path in rows}

    out: list[CitedChunk] = []
    display_i = 0
    for res in results:
        if res.entity_type != "chunk":
            continue
        pair = by_id.get(res.entity_id)
        if pair is None:
            continue
        chunk, path = pair
        display_i += 1
        out.append(
            CitedChunk(
                display_id=f"c{display_i}",
                chunk_id=chunk.id,
                file_path=path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                language=chunk.language,
                chunk_type=chunk.chunk_type,
                name=chunk.name,
                content=chunk.content,
                similarity=res.score,
                # Debug: per-component scores from the retriever (Slice 6i).
                score_breakdown=dict(res.score_breakdown) if res.score_breakdown else None,
            )
        )
    return out


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
