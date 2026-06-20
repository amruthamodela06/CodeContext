import asyncio
import json
import logging
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

import app.db as app_db
from app.chunking.orchestrator import chunk_repo
from app.citations import build_messages, parse, resolve
from app.citations.context import CitationContext
from app.db import get_session
from app.embeddings import get_embedder
from app.embeddings.orchestrator import embed_repo
from app.ingest import MAX_FILE_COUNT, FileCountExceededError, clone_repo, ingest_repo
from app.llm import get_llm_provider
from app.models import CodeChunk, EntityEmbedding, File, Repo
from app.schemas import (
    ChunkOut,
    ChunkSummary,
    ChunkTriggerResponse,
    EmbeddingStatusResponse,
    EmbedTriggerResponse,
    FileOut,
    IngestRequest,
    QueryRequest,
    QueryResponse,
    RepoChunksResponse,
    RepoFilesResponse,
    RepoOut,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

log = logging.getLogger(__name__)
router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --- Helpers --------------------------------------------------------------


async def _chunk_counts_by_file(repo_id: int, session: AsyncSession) -> dict[int, int]:
    """Return {file_id: chunk_count} for chunks belonging to this repo."""
    rows = await session.execute(
        select(CodeChunk.file_id, func.count(CodeChunk.id))
        .where(CodeChunk.repo_id == repo_id)
        .group_by(CodeChunk.file_id)
    )
    return dict(rows.all())


async def _serialize(repo: Repo, session: AsyncSession) -> RepoFilesResponse:
    """Build the file-list response, merging in per-file chunk counts."""
    counts = await _chunk_counts_by_file(repo.id, session)
    files = sorted(repo.files, key=lambda f: f.path)
    return RepoFilesResponse(
        repo=RepoOut.model_validate(repo),
        files=[
            FileOut(
                path=f.path,
                size_bytes=f.size_bytes,
                language=f.language,
                chunk_count=counts.get(f.id, 0),
            )
            for f in files
        ],
        file_count=len(files),
    )


# --- Ingest + files (Slice 1) --------------------------------------------


@router.post("/ingest", response_model=RepoFilesResponse)
async def ingest(
    req: IngestRequest,
    session: SessionDep,
) -> RepoFilesResponse:
    try:
        repo = await ingest_repo(req.url, session)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except FileCountExceededError as e:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Repository has {e.count} files; limit is {MAX_FILE_COUNT}.",
        ) from e
    except subprocess.CalledProcessError as e:
        log.warning("git clone failed: %s", (e.stderr or "").strip())
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="git clone failed; check the URL and that the repo is public.",
        ) from e
    return await _serialize(repo, session)


@router.get("/repos/{owner}/{name}/files", response_model=RepoFilesResponse)
async def list_files(
    owner: str,
    name: str,
    session: SessionDep,
) -> RepoFilesResponse:
    repo = await session.scalar(
        select(Repo).where(Repo.owner == owner, Repo.name == name).options(selectinload(Repo.files))
    )
    if repo is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"repo {owner}/{name} not ingested",
        )
    return await _serialize(repo, session)


# --- Chunks (Slice 2) -----------------------------------------------------


@router.post(
    "/repos/{repo_id}/chunk",
    response_model=ChunkTriggerResponse,
    description=(
        "Re-clone the repo and run the chunker. Idempotent — existing chunks "
        "for the repo are deleted first. Used to retry a failed auto-chunk "
        "or after changing chunking rules."
    ),
)
async def trigger_chunk(
    repo_id: int,
    session: SessionDep,
) -> ChunkTriggerResponse:
    repo = await session.scalar(
        select(Repo).where(Repo.id == repo_id).options(selectinload(Repo.files))
    )
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={repo_id} not found")

    clone_url = f"https://github.com/{repo.owner}/{repo.name}.git"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        dest = Path(tmp) / "repo"
        try:
            await asyncio.to_thread(clone_repo, clone_url, dest)
        except subprocess.CalledProcessError as e:
            log.warning("re-clone for chunking failed: %s", (e.stderr or "").strip())
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail="git clone failed; the repo may have been removed or made private.",
            ) from e
        count = await chunk_repo(repo, dest, session)

    return ChunkTriggerResponse(repo_id=repo_id, chunk_count=count)


@router.get("/repos/{repo_id}/chunks", response_model=RepoChunksResponse)
async def list_chunks(
    repo_id: int,
    session: SessionDep,
    chunk_type: Annotated[str | None, Query(description="Filter by chunk_type")] = None,
    language: Annotated[str | None, Query(description="Filter by language")] = None,
    file_id: Annotated[int | None, Query(description="Filter by file id")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RepoChunksResponse:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={repo_id} not found")

    filters = [CodeChunk.repo_id == repo_id]
    if chunk_type:
        filters.append(CodeChunk.chunk_type == chunk_type)
    if language:
        filters.append(CodeChunk.language == language)
    if file_id is not None:
        filters.append(CodeChunk.file_id == file_id)

    total = await session.scalar(select(func.count(CodeChunk.id)).where(*filters))

    page = await session.execute(
        select(CodeChunk)
        .where(*filters)
        .order_by(CodeChunk.file_id, CodeChunk.start_line)
        .limit(limit)
        .offset(offset)
    )
    chunks = list(page.scalars().all())

    by_type_rows = await session.execute(
        select(CodeChunk.chunk_type, func.count(CodeChunk.id))
        .where(*filters)
        .group_by(CodeChunk.chunk_type)
    )
    by_lang_rows = await session.execute(
        select(CodeChunk.language, func.count(CodeChunk.id))
        .where(*filters)
        .group_by(CodeChunk.language)
    )

    return RepoChunksResponse(
        repo_id=repo_id,
        chunks=[ChunkOut.model_validate(c) for c in chunks],
        total=total or 0,
        limit=limit,
        offset=offset,
        summary=ChunkSummary(
            by_type=dict(by_type_rows.all()),
            by_language=dict(by_lang_rows.all()),
        ),
    )


@router.get("/chunks/{chunk_id}", response_model=ChunkOut)
async def get_chunk(chunk_id: int, session: SessionDep) -> ChunkOut:
    chunk = await session.get(CodeChunk, chunk_id)
    if chunk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"chunk id={chunk_id} not found")
    return ChunkOut.model_validate(chunk)


# --- Embeddings + search (Slice 3) ----------------------------------------


@router.post(
    "/repos/{repo_id}/embed",
    response_model=EmbedTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    description=(
        "Embed all of a repo's chunks (CPU, bge-small). Runs in the background; "
        "returns 202 immediately. Poll GET /repos/{repo_id}/embedding-status. "
        "Idempotent — existing vectors for the repo are replaced."
    ),
)
async def trigger_embed(
    repo_id: int,
    session: SessionDep,
    background_tasks: BackgroundTasks,
) -> EmbedTriggerResponse:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={repo_id} not found")

    # Mark in_progress synchronously so an immediate status poll reflects it.
    repo.embedding_status = "in_progress"
    repo.embedding_progress = 0.0
    await session.commit()

    # app_db.SessionLocal resolved at call time so the test-swapped factory wins.
    background_tasks.add_task(embed_repo, repo_id, app_db.SessionLocal)
    return EmbedTriggerResponse(repo_id=repo_id, embedding_status="in_progress")


@router.get("/repos/{repo_id}/embedding-status", response_model=EmbeddingStatusResponse)
async def embedding_status(repo_id: int, session: SessionDep) -> EmbeddingStatusResponse:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={repo_id} not found")

    chunks_total = await session.scalar(
        select(func.count(CodeChunk.id)).where(CodeChunk.repo_id == repo_id)
    )
    chunks_embedded = await session.scalar(
        select(func.count(EntityEmbedding.id)).where(
            EntityEmbedding.repo_id == repo_id,
            EntityEmbedding.entity_type == "chunk",
        )
    )
    return EmbeddingStatusResponse(
        repo_id=repo_id,
        embedding_status=repo.embedding_status,
        embedding_progress=repo.embedding_progress,
        chunks_total=chunks_total or 0,
        chunks_embedded=chunks_embedded or 0,
    )


async def retrieve_chunks(
    session: AsyncSession, repo_id: int, query: str, top_k: int
) -> list[SearchResult]:
    """Embed `query` and return the top-k chunks for the repo by cosine
    similarity. Shared by /search and /query so both rank identically.
    """
    embedder = get_embedder()
    query_vec = await asyncio.to_thread(embedder.embed_one, query)

    # pgvector cosine distance (<=>). similarity = 1 - distance.
    # entity_embedding is polymorphic since Slice 5; filter to chunk rows in
    # the join so the cosine search only ranks code (commits/PRs/issues come
    # in via multi-hop expansion in Slice 5f, not flat retrieval).
    distance = EntityEmbedding.embedding.cosine_distance(query_vec).label("distance")
    rows = (
        await session.execute(
            select(CodeChunk, File.path, distance)
            .join(
                EntityEmbedding,
                (EntityEmbedding.entity_id == CodeChunk.id)
                & (EntityEmbedding.entity_type == "chunk"),
            )
            .join(File, File.id == CodeChunk.file_id)
            .where(EntityEmbedding.repo_id == repo_id)
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [
        SearchResult(
            chunk_id=chunk.id,
            similarity=1.0 - float(dist),
            file_path=path,
            chunk_type=chunk.chunk_type,
            name=chunk.name,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            language=chunk.language,
            content=chunk.content,
        )
        for chunk, path, dist in rows
    ]


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, session: SessionDep) -> SearchResponse:
    repo = await session.get(Repo, req.repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={req.repo_id} not found")

    results = await retrieve_chunks(session, req.repo_id, req.query, req.top_k)
    return SearchResponse(repo_id=req.repo_id, query=req.query, results=results)


def _sse(event: str, payload: dict) -> dict:
    return {"event": event, "data": json.dumps(payload)}


@router.post(
    "/query",
    description=(
        "Ask a question about the repo. Retrieves top-k chunks, streams a cited "
        "answer from the configured LLM (SSE), then emits resolved citations. "
        "Citation ids (`[chunk:c1]`) are validated mechanically against the "
        "retrieved set — see ADR 0010."
    ),
)
async def query(req: QueryRequest, session: SessionDep):
    repo = await session.get(Repo, req.repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={req.repo_id} not found")

    # All DB access happens here, before streaming begins; the SSE generator
    # below is pure in-memory work (LLM stream + citation parsing), so it never
    # touches the request-scoped session after the response starts.
    results = await retrieve_chunks(session, req.repo_id, req.question, req.top_k)
    ctx = CitationContext.from_results(results)
    messages = build_messages(repo.owner, repo.name, req.question, ctx)
    provider = get_llm_provider()
    owner, name = repo.owner, repo.name
    ref = repo.commit_sha or repo.default_branch  # SHA-pinned when available (§9.4)

    if not req.stream:
        gen = await provider.generate(messages)
        cites, warnings = parse(gen.text)
        resolved = resolve(cites, ctx, owner=owner, name=name, ref=ref)
        return QueryResponse(
            repo_id=req.repo_id,
            question=req.question,
            answer=gen.text,
            citations=resolved,
            warnings=warnings,
            sources=ctx.chunks,
        )

    async def event_gen() -> AsyncIterator[dict]:
        # All retrieved chunks up front: powers the Sources panel and supplies
        # the chunk bodies the UI shows when a citation is expanded.
        yield _sse("sources", {"sources": [c.model_dump() for c in ctx.chunks]})

        parts: list[str] = []
        try:
            async for delta in provider.generate_stream(messages):
                parts.append(delta)
                yield _sse("token", {"text": delta})
        except Exception as exc:
            # Surface any LLM failure to the client as an error event.
            # CancelledError is BaseException, not Exception, so a client
            # disconnect propagates past this handler and the provider's
            # `finally` closes the upstream stream (ADR 0010).
            log.warning("LLM stream failed: %s", exc)
            yield _sse("error", {"message": str(exc), "stage": "llm"})
            return

        answer = "".join(parts)
        cites, warnings = parse(answer)
        resolved = resolve(cites, ctx, owner=owner, name=name, ref=ref)
        yield _sse(
            "citations",
            {"citations": [r.model_dump() for r in resolved], "warnings": warnings},
        )
        yield _sse("done", {})

    return EventSourceResponse(event_gen(), headers={"X-Accel-Buffering": "no"})
