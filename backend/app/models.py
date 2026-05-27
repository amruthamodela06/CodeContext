from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repo"
    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repo_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    default_branch: Mapped[str] = mapped_column(String(128))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    files: Mapped[list["File"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    code_chunks: Mapped[list["CodeChunk"]] = relationship(
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
