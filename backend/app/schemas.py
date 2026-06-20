from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.citations.context import CitedChunk
from app.citations.validator import ResolvedCitation

# --- Ingest -------------------------------------------------------------


class IngestRequest(BaseModel):
    url: str = Field(
        ...,
        description="Public GitHub repo URL, e.g. https://github.com/octocat/Hello-World",
    )


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    size_bytes: int
    language: str | None
    chunk_count: int = 0


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner: str
    name: str
    default_branch: str
    commit_sha: str | None = None
    ingested_at: datetime


class RepoFilesResponse(BaseModel):
    repo: RepoOut
    files: list[FileOut]
    file_count: int


# --- Chunks -------------------------------------------------------------

# Allowed chunk types — kept open at the DB layer (String(32)) but enforced
# at the API edge via this Literal. Mirrors app.chunking.protocol.ChunkType.
ChunkTypeLiteral = Literal[
    "module_docstring",
    "module_preamble",
    "function",
    "class",
    "method",
    "top_level_block",
    "interface_decl",
    "type_alias",
    "struct_decl",
    "enum_decl",
    "trait_decl",
    "macro_def",
]


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: int
    chunk_type: str
    name: str | None
    parent_name: str | None
    start_line: int
    end_line: int
    language: str
    is_async: bool
    extra_metadata: dict[str, Any]
    content: str
    created_at: datetime


class ChunkSummary(BaseModel):
    """Aggregate counts surfaced alongside a chunk list."""

    by_type: dict[str, int]
    by_language: dict[str, int]


class RepoChunksResponse(BaseModel):
    repo_id: int
    chunks: list[ChunkOut]
    total: int
    limit: int
    offset: int
    summary: ChunkSummary


class ChunkTriggerResponse(BaseModel):
    """Returned by POST /repos/{repo_id}/chunk."""

    repo_id: int
    chunk_count: int


# --- Embeddings + search (Slice 3) --------------------------------------


class EmbedTriggerResponse(BaseModel):
    """Returned by POST /repos/{repo_id}/embed (202 — work runs in background)."""

    repo_id: int
    embedding_status: str


class EmbeddingStatusResponse(BaseModel):
    repo_id: int
    embedding_status: str  # pending | in_progress | done | failed
    embedding_progress: float  # 0.0 - 1.0
    chunks_total: int
    chunks_embedded: int


# --- History ingestion (Slice 5b, ADR 0011) -----------------------------


class HistoryIngestionTriggerResponse(BaseModel):
    """Returned by POST /repos/{repo_id}/ingest-history (202)."""

    repo_id: int
    history_ingestion_status: str


class HistoryIngestionStatusResponse(BaseModel):
    repo_id: int
    history_ingestion_status: str  # pending | in_progress | done | failed
    history_ingestion_progress: float
    commits_count: int
    pull_requests_count: int
    issues_count: int
    # Latest error message captured in repo.history_ingestion_state, if any.
    error: str | None = None


class SearchRequest(BaseModel):
    repo_id: int
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)


class SearchResult(BaseModel):
    chunk_id: int
    similarity: float
    file_path: str
    chunk_type: str
    name: str | None
    start_line: int
    end_line: int
    language: str
    content: str


class SearchResponse(BaseModel):
    repo_id: int
    query: str
    results: list[SearchResult]


# --- Query / cited answers (Slice 4, ADR 0010) --------------------------


class QueryRequest(BaseModel):
    repo_id: int
    question: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    stream: bool = True


class QueryResponse(BaseModel):
    """Non-streaming fallback shape (stream=false). The streaming path emits the
    same data across SSE `sources` / `citations` events instead.
    """

    repo_id: int
    question: str
    answer: str
    citations: list[ResolvedCitation]
    warnings: list[str]
    sources: list[CitedChunk]
