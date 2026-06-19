"""Per-query citation context: maps short display ids -> chunk metadata + content.

Built fresh per query from the retrieval rows. Passed to both the prompt builder
(to render the excerpt block the LLM cites against) and the validator (which
treats membership here as ground truth). Display ids are sequential per query
(c1..cN, rank order) — see ADR 0010 for why sequential over hash-based.
"""

from collections.abc import Iterable, Sequence

from pydantic import BaseModel


class CitedChunk(BaseModel):
    display_id: str  # query-local, e.g. "c1"
    chunk_id: int  # CodeChunk.id — stable across queries
    file_path: str
    start_line: int
    end_line: int
    language: str | None
    chunk_type: str
    name: str | None
    content: str
    similarity: float


class CitationContext:
    """Holds the retrieved chunks for one query and their display-id index."""

    def __init__(self, chunks: Sequence[CitedChunk]) -> None:
        self.chunks: list[CitedChunk] = list(chunks)
        self.by_display_id: dict[str, CitedChunk] = {c.display_id: c for c in self.chunks}

    @classmethod
    def from_results(cls, results: Iterable, start_index: int = 1) -> "CitationContext":
        """Build from retrieval results (objects with the SearchResult fields),
        assigning sequential display ids c1..cN in iteration (rank) order.
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
        return cls(chunks)

    def render_excerpts(self) -> str:
        """The excerpt block embedded in the user message. Each chunk is shown
        under its display id, with file/line metadata and a fenced code body.
        """
        blocks: list[str] = []
        for c in self.chunks:
            meta = c.chunk_type + (f" {c.name}" if c.name else "")
            header = f"[{c.display_id}] {c.file_path}:{c.start_line}-{c.end_line} ({meta})"
            lang = (c.language or "").lower()
            blocks.append(f"{header}\n```{lang}\n{c.content}\n```")
        return "\n\n".join(blocks)
