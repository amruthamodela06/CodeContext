"""Repo-level chunking orchestrator.

Walks the cloned repo on disk, dispatches each supported file to a per-language
chunker, and persists the resulting CodeChunk rows. Idempotent — re-runs delete
any existing chunks for the repo first (via the same session).

This module needs the cloned repo on disk because we don't store file content
in the DB this slice. The auto-chunk hook in `ingest_repo` calls us before the
tempdir is cleaned up. The explicit POST /repos/{repo_id}/chunk endpoint
re-clones first.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.chunking import chunker_for
from app.models import CodeChunk, Repo

log = logging.getLogger(__name__)


async def chunk_repo(repo: Repo, repo_root: Path, session: AsyncSession) -> int:
    """Chunk every supported file under ``repo_root`` and persist.

    Deletes any existing chunks for this repo first, then inserts fresh ones.
    Per-file parse/IO errors are logged and skipped — never raised. Stub
    languages (TS/JS/Go/Rust this slice) are also silently skipped via the
    NotImplementedError catch.

    Returns the number of chunks persisted.
    """
    await session.execute(delete(CodeChunk).where(CodeChunk.repo_id == repo.id))
    await session.flush()

    total = 0
    for file in repo.files:
        chunker = chunker_for(file.language)
        if chunker is None:
            continue

        source_path = repo_root / file.path
        try:
            source = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("could not read %s: %s", source_path, exc)
            continue

        try:
            chunks = chunker.chunk(source)
        except NotImplementedError:
            continue  # stub language; skip silently
        except Exception as exc:
            log.warning("chunking %s failed: %s", file.path, exc)
            continue

        for chunk in chunks:
            session.add(
                CodeChunk(
                    repo_id=repo.id,
                    file_id=file.id,
                    chunk_type=chunk.chunk_type,
                    name=chunk.name,
                    parent_name=chunk.parent_name,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    content=chunk.content,
                    language=chunk.language,
                    is_async=chunk.is_async,
                    extra_metadata=chunk.extra_metadata,
                )
            )
        total += len(chunks)

    await session.commit()
    return total
