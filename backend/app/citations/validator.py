"""Classifies parsed citations against a CitationContext and resolves them.

Membership in the context is the ground truth: a parsed id resolves to a real
chunk (status 'valid'), is the explicit no-citation sentinel ('none'), or is
unknown ('invalid'). Invalid ids are surfaced, never dropped. Valid ids resolve
to file/line + a SHA-pinned permalink (PRD §9.4). See ADR 0010.

Limitation (accepted for v1): this proves a cited id maps to a real chunk; it
does NOT prove the chunk semantically supports the specific claim. Semantic
citation accuracy is measured in the Slice 7 eval, not enforced here.
"""

from typing import Literal

from pydantic import BaseModel

from app.citations.context import CitationContext
from app.citations.parser import ParsedCitation

CitationStatus = Literal["valid", "none", "invalid"]


class ResolvedCitation(BaseModel):
    display_id: str
    status: CitationStatus
    chunk_id: int | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    permalink: str | None = None


def _permalink(owner: str, name: str, ref: str, path: str, start: int, end: int) -> str:
    return f"https://github.com/{owner}/{name}/blob/{ref}/{path}#L{start}-L{end}"


def resolve(
    parsed: list[ParsedCitation],
    ctx: CitationContext,
    *,
    owner: str,
    name: str,
    ref: str,
) -> list[ResolvedCitation]:
    """De-duplicate by display id (first-appearance order) and classify each.

    `ref` is the commit SHA when available (preferred — immutable), else the
    branch name as a fallback for repos ingested before SHA capture.
    """
    seen: dict[str, ResolvedCitation] = {}
    for p in parsed:
        if p.display_id in seen:
            continue
        seen[p.display_id] = _classify(p.display_id, ctx, owner, name, ref)
    return list(seen.values())


def _classify(
    display_id: str, ctx: CitationContext, owner: str, name: str, ref: str
) -> ResolvedCitation:
    if display_id == "none":
        return ResolvedCitation(display_id="none", status="none")
    chunk = ctx.by_display_id.get(display_id)
    if chunk is None:
        return ResolvedCitation(display_id=display_id, status="invalid")
    return ResolvedCitation(
        display_id=display_id,
        status="valid",
        chunk_id=chunk.chunk_id,
        file_path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        permalink=_permalink(owner, name, ref, chunk.file_path, chunk.start_line, chunk.end_line),
    )
