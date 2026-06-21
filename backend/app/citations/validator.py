"""Classifies parsed citations against a CitationContext and resolves them.

Membership in the context is the ground truth. A parsed (entity_type,
display_id) pair resolves to a real entity (status 'valid'), is the
explicit no-citation sentinel ('none'), or is unknown ('invalid').
Invalid IDs are surfaced, never dropped. Valid IDs resolve to a
type-specific GitHub permalink (chunk -> blob+lines, commit -> /commit,
pr -> /pull, issue -> /issues).

Limitation (accepted for v1): this proves the cited ID maps to a real
entity; it does NOT prove the entity semantically supports the specific
claim. Semantic citation accuracy is measured in the Slice 7 eval, not
enforced at query time. See ADR 0010 + 0012.
"""

from typing import Literal

from pydantic import BaseModel

from app.citations.context import (
    CitationContext,
    CitedChunk,
    CitedCommit,
    CitedIssue,
    CitedPR,
)
from app.citations.parser import ParsedCitation

CitationStatus = Literal["valid", "none", "invalid"]


class ResolvedCitation(BaseModel):
    entity_type: str = "chunk"  # 'chunk' | 'commit' | 'pr' | 'issue'
    display_id: str
    status: CitationStatus
    # Type-specific identifying fields (only one of these is set per row).
    chunk_id: int | None = None
    commit_sha: str | None = None
    pr_number: int | None = None
    issue_number: int | None = None
    # Shared display fields.
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    title: str | None = None
    permalink: str | None = None


def _chunk_permalink(owner, name, ref, path, start, end) -> str:
    return f"https://github.com/{owner}/{name}/blob/{ref}/{path}#L{start}-L{end}"


def _commit_permalink(owner, name, sha) -> str:
    return f"https://github.com/{owner}/{name}/commit/{sha}"


def _pr_permalink(owner, name, number) -> str:
    return f"https://github.com/{owner}/{name}/pull/{number}"


def _issue_permalink(owner, name, number) -> str:
    return f"https://github.com/{owner}/{name}/issues/{number}"


def resolve(
    parsed: list[ParsedCitation],
    ctx: CitationContext,
    *,
    owner: str,
    name: str,
    ref: str,
) -> list[ResolvedCitation]:
    """De-duplicate by (entity_type, display_id) and classify each.

    ``ref`` is the commit SHA when available (preferred -- immutable),
    else the branch name. Used only for chunk permalinks (the other
    types have stable type-specific URLs).
    """
    seen: dict[tuple[str, str], ResolvedCitation] = {}
    for p in parsed:
        key = (p.entity_type, p.display_id)
        if key in seen:
            continue
        seen[key] = _classify(p, ctx, owner, name, ref)
    return list(seen.values())


def _classify(
    p: ParsedCitation,
    ctx: CitationContext,
    owner: str,
    name: str,
    ref: str,
) -> ResolvedCitation:
    if p.display_id == "none":
        # The `none` sentinel is type-agnostic -- a model emitting any
        # [<type>:none] indicates "no supporting source" for that claim.
        return ResolvedCitation(entity_type=p.entity_type, display_id="none", status="none")

    entity = ctx.by_token.get((p.entity_type, p.display_id))
    if entity is None:
        return ResolvedCitation(
            entity_type=p.entity_type, display_id=p.display_id, status="invalid"
        )

    base = ResolvedCitation(entity_type=p.entity_type, display_id=p.display_id, status="valid")
    if isinstance(entity, CitedChunk):
        return base.model_copy(
            update={
                "chunk_id": entity.chunk_id,
                "file_path": entity.file_path,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
                "permalink": _chunk_permalink(
                    owner, name, ref, entity.file_path, entity.start_line, entity.end_line
                ),
            }
        )
    if isinstance(entity, CitedCommit):
        return base.model_copy(
            update={
                "commit_sha": entity.sha,
                "title": entity.message.split("\n", 1)[0][:120],
                "permalink": _commit_permalink(owner, name, entity.sha),
            }
        )
    if isinstance(entity, CitedPR):
        return base.model_copy(
            update={
                "pr_number": entity.number,
                "title": entity.title,
                "permalink": _pr_permalink(owner, name, entity.number),
            }
        )
    if isinstance(entity, CitedIssue):
        return base.model_copy(
            update={
                "issue_number": entity.number,
                "title": entity.title,
                "permalink": _issue_permalink(owner, name, entity.number),
            }
        )
    return base  # unreachable; CitableEntity union is closed
