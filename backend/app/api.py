import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.db as app_db
from app.chunking.orchestrator import chunk_repo
from app.db import get_session
from app.embeddings import get_embedder
from app.embeddings.orchestrator import embed_repo
from app.ingest import MAX_FILE_COUNT, FileCountExceededError, clone_repo, ingest_repo
from app.models import ChunkEmbedding, CodeChunk, File, Repo
from app.schemas import (
    ChunkOut,
    ChunkSummary,
    ChunkTriggerResponse,
    EmbeddingStatusResponse,
    EmbedTriggerResponse,
    FileOut,
    IngestRequest,
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
        select(func.count(ChunkEmbedding.id)).where(ChunkEmbedding.repo_id == repo_id)
    )
    return EmbeddingStatusResponse(
        repo_id=repo_id,
        embedding_status=repo.embedding_status,
        embedding_progress=repo.embedding_progress,
        chunks_total=chunks_total or 0,
        chunks_embedded=chunks_embedded or 0,
    )


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, session: SessionDep) -> SearchResponse:
    repo = await session.get(Repo, req.repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"repo id={req.repo_id} not found")

    embedder = get_embedder()
    query_vec = await asyncio.to_thread(embedder.embed_one, req.query)

    # pgvector cosine distance (<=>). similarity = 1 - distance.
    distance = ChunkEmbedding.embedding.cosine_distance(query_vec).label("distance")
    rows = (
        await session.execute(
            select(CodeChunk, File.path, distance)
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == CodeChunk.id)
            .join(File, File.id == CodeChunk.file_id)
            .where(ChunkEmbedding.repo_id == req.repo_id)
            .order_by(distance)
            .limit(req.top_k)
        )
    ).all()

    results = [
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
    return SearchResponse(repo_id=req.repo_id, query=req.query, results=results)
