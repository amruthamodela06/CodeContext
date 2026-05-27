"""Chunker interface + Chunk value type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

ChunkType = Literal[
    # Implemented (Python) this slice:
    "module_docstring",
    "module_preamble",
    "function",
    "class",
    "method",
    "top_level_block",
    # Reserved for stub languages (TS/JS/Go/Rust); not emitted this slice:
    "interface_decl",
    "type_alias",
    "struct_decl",
    "enum_decl",
    "trait_decl",
    "macro_def",
]


@dataclass(frozen=True)
class Chunk:
    """A semantically meaningful slice of source. See ADR 0008 for the rules.

    `start_line` and `end_line` are 1-indexed and inclusive (matching how
    citations are shown to users). Tree-sitter's 0-indexed rows are converted
    at the chunker boundary; downstream code must not re-convert.
    """

    chunk_type: ChunkType
    name: str | None
    parent_name: str | None
    start_line: int
    end_line: int
    content: str
    language: str
    is_async: bool = False
    extra_metadata: dict[str, Any] = field(default_factory=dict)


class Chunker(ABC):
    """Abstract chunker. One implementation per language."""

    language: str  # human-readable label (e.g. "Python"), set on the subclass

    @abstractmethod
    def chunk(self, source: str) -> list[Chunk]:
        """Parse `source` and return a list of Chunks.

        Implementations MUST:
        - Normalize CRLF/CR line endings to LF before parsing.
        - Return an empty list (not raise) on parse failure or unrecoverable
          tree-sitter errors. The caller logs and moves on.
        """
