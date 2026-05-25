import logging
import subprocess
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.ingest import MAX_FILE_COUNT, FileCountExceededError, ingest_repo
from app.models import Repo
from app.schemas import FileOut, IngestRequest, RepoFilesResponse, RepoOut

log = logging.getLogger(__name__)
router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _serialize(repo: Repo) -> RepoFilesResponse:
    """Build the response from a loaded Repo + its files relationship."""
    files = sorted(repo.files, key=lambda f: f.path)
    return RepoFilesResponse(
        repo=RepoOut.model_validate(repo),
        files=[FileOut.model_validate(f) for f in files],
        file_count=len(files),
    )


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
    return _serialize(repo)


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
    return _serialize(repo)
