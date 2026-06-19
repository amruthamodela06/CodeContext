from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Embedding vector dimension. bge-small-en-v1.5 is 384-dim (ADR 0009).
# Fixed at column-creation time; a model change with a different dimension
# means a migration + re-embed (startup asserts the active embedder matches).
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repo"
    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repo_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    default_branch: Mapped[str] = mapped_column(String(128))
    # HEAD commit at ingestion time. Nullable so repos ingested before Slice 4
    # (no SHA captured) still load; citation permalinks fall back to the branch
    # ref when null. See ADR 0010 / PRD §9.4.
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Embedding job lifecycle (ADR 0009). pending | in_progress | done | failed.
    embedding_status: Mapped[str] = mapped_column(
        String(16), server_default="pending", nullable=False
    )
    embedding_progress: Mapped[float] = mapped_column(Float, server_default="0.0", nullable=False)

    files: Mapped[list["File"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    code_chunks: Mapped[list["CodeChunk"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    chunk_embeddings: Mapped[list["ChunkEmbedding"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )


class File(Base):
    __tablename__ = "file"
    __table_args__ = (UniqueConstraint("repo_id", "path", name="uq_file_repo_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    language: Mapped[str | None] = mapped_column(String(32))

    repo: Mapped[Repo] = relationship(back_populates="files")
    code_chunks: Mapped[list["CodeChunk"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class CodeChunk(Base):
    """A semantically meaningful unit of source code extracted via tree-sitter.

    See ADR 0008 for chunking rules and line-numbering conventions.
    """

    __tablename__ = "code_chunk"
    __table_args__ = (
        # Filter "all chunks for a repo by type" without joining file.
        # Justifies keeping the denormalized repo_id column.
        Index("ix_code_chunk_repo_type", "repo_id", "chunk_type"),
        # Natural ordering within a file (sort/scan by line).
        Index("ix_code_chunk_file_start", "file_id", "start_line"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("file.id", ondelete="CASCADE"), index=True)
    # chunk_type is open-ended at the DB layer (String, not ENUM) so we can add
    # values without a migration. Allowed values are enforced by Pydantic
    # Literal[...] at the API edge.
    chunk_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(255))
    parent_name: Mapped[str | None] = mapped_column(String(255))
    # 1-indexed and inclusive. Conversion from tree-sitter's 0-indexed rows
    # happens at the chunker boundary — see ADR 0008.
    start_line: Mapped[int] = mapped_column()
    end_line: Mapped[int] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(32))
    is_async: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    extra_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="code_chunks")
    file: Mapped["File"] = relationship(back_populates="code_chunks")
    chunk_embeddings: Mapped[list["ChunkEmbedding"]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan"
    )


class ChunkEmbedding(Base):
    """A dense vector embedding of a CodeChunk. See ADR 0009.

    Separate table (not a column on code_chunk) so multiple models can coexist
    for ablation. The vector dimension is fixed at column creation; re-embed
    deletes the repo's existing rows then re-inserts. The HNSW index is built by
    the embed orchestrator after bulk insert, NOT in the migration.
    """

    __tablename__ = "chunk_embedding"
    __table_args__ = (
        # Repo-scoped filtering without joining code_chunk.
        Index("ix_chunk_embedding_repo_model", "repo_id", "model_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("code_chunk.id", ondelete="CASCADE"), index=True
    )
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    model_name: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    chunk: Mapped[CodeChunk] = relationship(back_populates="chunk_embeddings")
    repo: Mapped[Repo] = relationship(back_populates="chunk_embeddings")
