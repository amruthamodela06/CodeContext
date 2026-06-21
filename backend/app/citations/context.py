"""Per-query citation context: maps short (entity_type, display_id) tokens
to chunk / commit / PR / issue rows. Built fresh per query.

Slice 4 held only chunks (`[chunk:c1]`). Slice 5f widens to four entity
types using the same shape: display IDs are sequential per type with a
type-prefixed letter (c1/c2/m1/m2/p1/p2/i1/i2) so the LLM can emit
`[chunk:c1]`, `[commit:m1]`, `[pr:p1]`, `[issue:i1]` in prose. See ADR 0010
+ 0012.

The same context is passed to both the prompt builder (renders typed
excerpts the model cites against) and the validator (treats membership
here as ground truth -- an ID in the answer that isn't in by_token is
`invalid`).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

EntityType = Literal["chunk", "commit", "pr", "issue"]


# ---- Per-type citable entities -------------------------------------------


class CitedChunk(BaseModel):
    type: Literal["chunk"] = "chunk"
    display_id: str  # "c1", "c2", ...
    chunk_id: int  # CodeChunk.id (stable across queries)
    file_path: str
    start_line: int
    end_line: int
    language: str | None
    chunk_type: str
    name: str | None
    content: str
    similarity: float


class CitedCommit(BaseModel):
    type: Literal["commit"] = "commit"
    display_id: str  # "m1", "m2", ...
    commit_id: int  # Commit.id
    sha: str
    author_name: str | None
    authored_at: datetime | None
    message: str
    similarity: float = 0.0  # filled by reranker for multi-hop


class CitedPR(BaseModel):
    type: Literal["pr"] = "pr"
    display_id: str  # "p1", "p2", ...
    pr_id: int  # PullRequest.id
    number: int
    title: str
    body: str | None
    state: str
    merged_at: datetime | None
    similarity: float = 0.0


class CitedIssue(BaseModel):
    type: Literal["issue"] = "issue"
    display_id: str  # "i1", "i2", ...
    issue_id: int  # Issue.id
    number: int
    title: str
    body: str | None
    state: str
    closed_at: datetime | None
    similarity: float = 0.0


# Discriminated-union alias for typing convenience.
CitableEntity = CitedChunk | CitedCommit | CitedPR | CitedIssue


# ---- Context -------------------------------------------------------------


class CitationContext:
    """Holds the retrieved entities for one query and a (type, id) -> entity
    index. Validator + parser key off ``by_token``; renderer iterates the
    typed lists in order.
    """

    def __init__(
        self,
        chunks: list[CitedChunk] | None = None,
        *,
        commits: list[CitedCommit] | None = None,
        prs: list[CitedPR] | None = None,
        issues: list[CitedIssue] | None = None,
        # Optional per-entity relationship hints rendered in excerpt headers
        # ({(entity_type, display_id) -> "introduced by [c1]" style phrase}).
        # Filled by the multi-hop orchestrator after edges are walked.
        relations: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.chunks: list[CitedChunk] = list(chunks or [])
        self.commits: list[CitedCommit] = list(commits or [])
        self.prs: list[CitedPR] = list(prs or [])
        self.issues: list[CitedIssue] = list(issues or [])
        self.relations: dict[tuple[str, str], str] = dict(relations or {})

        self.by_token: dict[tuple[str, str], CitableEntity] = {}
        for c in self.chunks:
            self.by_token[("chunk", c.display_id)] = c
        for m in self.commits:
            self.by_token[("commit", m.display_id)] = m
        for p in self.prs:
            self.by_token[("pr", p.display_id)] = p
        for i in self.issues:
            self.by_token[("issue", i.display_id)] = i

    # --- Builders ---------------------------------------------------------

    @classmethod
    def from_results(cls, results, start_index: int = 1) -> "CitationContext":
        """Slice-4-compatible: build a chunks-only context from rank-ordered
        SearchResult-like rows. Display IDs assigned c1..cN.
        """
        chunks = [
            CitedChunk(
                display_id=f"c{start_index + i}",
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
        return cls(chunks=chunks)

    # --- Rendering --------------------------------------------------------

    def render_excerpts(self) -> str:
        """Render the excerpt block for the user message. Each entity gets
        a typed header (with any relationship phrase from ``relations``)
        followed by its body. Order: chunks, then commits, PRs, issues so
        the model sees code first.
        """
        blocks: list[str] = []
        for c in self.chunks:
            blocks.append(self._render_chunk(c))
        for m in self.commits:
            blocks.append(self._render_commit(m))
        for p in self.prs:
            blocks.append(self._render_pr(p))
        for i in self.issues:
            blocks.append(self._render_issue(i))
        return "\n\n".join(blocks)

    def _header(self, entity_type: str, display_id: str, base: str) -> str:
        rel = self.relations.get((entity_type, display_id))
        suffix = f"  ({rel})" if rel else ""
        return f"[{display_id}] {base}{suffix}"

    def _render_chunk(self, c: CitedChunk) -> str:
        meta = c.chunk_type + (f" {c.name}" if c.name else "")
        header = self._header(
            "chunk", c.display_id, f"code: {c.file_path}:{c.start_line}-{c.end_line} ({meta})"
        )
        lang = (c.language or "").lower()
        return f"{header}\n```{lang}\n{c.content}\n```"

    def _render_commit(self, m: CitedCommit) -> str:
        author = m.author_name or "unknown"
        when = m.authored_at.date().isoformat() if m.authored_at else "unknown"
        header = self._header("commit", m.display_id, f"commit: {m.sha[:7]} by {author} on {when}")
        return f"{header}\nMessage: {m.message.strip()}"

    def _render_pr(self, p: CitedPR) -> str:
        when = p.merged_at.date().isoformat() if p.merged_at else "open"
        header = self._header(
            "pr", p.display_id, f'pr: #{p.number} "{p.title}" ({p.state}, {when})'
        )
        body = (p.body or "").strip()
        body_excerpt = body[:500] + ("..." if len(body) > 500 else "")
        return f"{header}\nBody: {body_excerpt}" if body_excerpt else header

    def _render_issue(self, i: CitedIssue) -> str:
        when = i.closed_at.date().isoformat() if i.closed_at else "open"
        header = self._header(
            "issue", i.display_id, f'issue: #{i.number} "{i.title}" ({i.state}, {when})'
        )
        body = (i.body or "").strip()
        body_excerpt = body[:500] + ("..." if len(body) > 500 else "")
        return f"{header}\nBody: {body_excerpt}" if body_excerpt else header
