"""AST-aware code chunking via tree-sitter. See ADR 0008."""

from app.chunking.protocol import Chunk, Chunker, ChunkType
from app.chunking.python import PythonChunker
from app.chunking.stubs import (
    GoChunker,
    JavaScriptChunker,
    RustChunker,
    TypeScriptChunker,
)

# Map File.language values (case-insensitive) to a Chunker class.
_REGISTRY: dict[str, type[Chunker]] = {
    "python": PythonChunker,
    "typescript": TypeScriptChunker,
    "javascript": JavaScriptChunker,
    "go": GoChunker,
    "rust": RustChunker,
}


def chunker_for(language: str | None) -> Chunker | None:
    """Return a chunker for the given language name, or None if unsupported.

    `language` is the human-readable label stored in File.language ("Python",
    "TypeScript", ...). None inputs (files we couldn't classify) return None.
    """
    if not language:
        return None
    cls = _REGISTRY.get(language.lower())
    return cls() if cls else None


__all__ = [
    "Chunk",
    "ChunkType",
    "Chunker",
    "GoChunker",
    "JavaScriptChunker",
    "PythonChunker",
    "RustChunker",
    "TypeScriptChunker",
    "chunker_for",
]
