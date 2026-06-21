from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    # History ingestion job lifecycle (Slice 5, ADR 0011). Mirrors the embedding
    # pattern: pending | in_progress | done | failed. `state` is a JSONB blob the
    # background job uses to persist GraphQL cursors / counts so it can resume
    # after a rate-limit pause or daemon restart.
    history_ingestion_status: Mapped[str] = mapped_column(
        String(16), server_default="pending", nullable=False
    )
    history_ingestion_progress: Mapped[float] = mapped_column(
        Float, server_default="0.0", nullable=False
    )
    history_ingestion_state: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Graph-build job lifecycle (Slice 5c, ADR 0012). Depends on both chunking
    # (Slice 2) and history ingestion (Slice 5b) being done; populates
    # entity_edge with the four cross-domain edge types.
    graph_status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    graph_progress: Mapped[float] = mapped_column(Float, server_default="0.0", nullable=False)
    graph_state: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    files: Mapped[list["File"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    code_chunks: Mapped[list["CodeChunk"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    entity_embeddings: Mapped[list["EntityEmbedding"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    commits: Mapped[list["Commit"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    pull_requests: Mapped[list["PullRequest"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    issues: Mapped[list["Issue"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    entity_edges: Mapped[list["EntityEdge"]] = relationship(
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


class EntityEmbedding(Base):
    """A dense vector embedding of a citable entity. See ADR 0009 + 0013.

    Polymorphic: `entity_type` is one of 'chunk' | 'commit' | 'pr' | 'issue',
    `entity_id` is the PK in the corresponding table. No FK because the target
    table varies; integrity is the ingester's responsibility.

    Was `chunk_embedding` in Slice 3 (FK-bound to code_chunk). Slice 5 widens
    it to embed commits / PRs / issues alongside chunks so a single retrieval
    query (and a single HNSW index) covers all entity types.
    """

    __tablename__ = "entity_embedding"
    __table_args__ = (
        # Repo-scoped filtering without joining the entity tables.
        Index("ix_entity_embedding_repo_model", "repo_id", "model_name"),
        # Polymorphic lookups (e.g. "all chunk embeddings for repo R") and
        # de-dup checks before insert.
        Index("ix_entity_embedding_repo_type_id", "repo_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(16))  # 'chunk'|'commit'|'pr'|'issue'
    entity_id: Mapped[int] = mapped_column()  # PK in the corresponding entity table
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    model_name: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="entity_embeddings")


# --- Slice 5: history (commits / PRs / issues + comments) -------------------


class Commit(Base):
    """A git commit. Populated from local clone + GitHub GraphQL stats.

    Blame may attribute a chunk to a commit older than the ingested-history
    window, in which case the orchestrator inserts a stub row (sha + author +
    authored_at + message — everything readable from the local clone) so the
    `chunk -[introduced_by]-> commit` edge always has a valid target.
    See ADR 0011.
    """

    __tablename__ = "commit"
    __table_args__ = (
        UniqueConstraint("repo_id", "sha", name="uq_commit_repo_sha"),
        Index("ix_commit_repo_authored", "repo_id", "authored_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    sha: Mapped[str] = mapped_column(String(40))
    author_name: Mapped[str | None] = mapped_column(String(255))
    author_email: Mapped[str | None] = mapped_column(String(255))
    authored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str] = mapped_column(Text)
    parent_shas: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    files_changed_count: Mapped[int | None] = mapped_column()
    additions: Mapped[int | None] = mapped_column()
    deletions: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="commits")


class PullRequest(Base):
    """A GitHub pull request. See ADR 0011."""

    __tablename__ = "pull_request"
    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="uq_pr_repo_number"),
        Index("ix_pr_repo_merged", "repo_id", "merged_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column()
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16))  # open | closed | merged
    author: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merge_commit_sha: Mapped[str | None] = mapped_column(String(40))
    base_branch: Mapped[str | None] = mapped_column(String(255))
    head_branch: Mapped[str | None] = mapped_column(String(255))
    additions: Mapped[int | None] = mapped_column()
    deletions: Mapped[int | None] = mapped_column()
    files_changed_count: Mapped[int | None] = mapped_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="pull_requests")
    comments: Mapped[list["PRComment"]] = relationship(
        back_populates="pr", cascade="all, delete-orphan"
    )


class PRComment(Base):
    """A comment on a pull request — issue-style, inline review, or review body."""

    __tablename__ = "pr_comment"
    __table_args__ = (UniqueConstraint("pr_id", "github_id", name="uq_pr_comment_github"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_request.id", ondelete="CASCADE"), index=True
    )
    github_id: Mapped[int] = mapped_column(BigInteger)
    author: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 'issue_comment' (general PR comment), 'review_comment' (inline diff
    # comment), 'review_body' (top-level review submission text).
    comment_type: Mapped[str] = mapped_column(String(32))

    pr: Mapped[PullRequest] = relationship(back_populates="comments")


class Issue(Base):
    """A GitHub issue (non-PR). `closing_pr_number` is filled by edge
    construction (5c) from PR-body parsing for "fixes #N" / "closes #N".
    """

    __tablename__ = "issue"
    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="uq_issue_repo_number"),
        Index("ix_issue_repo_state", "repo_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column()
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16))  # open | closed
    author: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    labels: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    closing_pr_number: Mapped[int | None] = mapped_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="issues")
    comments: Mapped[list["IssueComment"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan"
    )


class IssueComment(Base):
    __tablename__ = "issue_comment"
    __table_args__ = (UniqueConstraint("issue_id", "github_id", name="uq_issue_comment_github"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issue.id", ondelete="CASCADE"), index=True)
    github_id: Mapped[int] = mapped_column(BigInteger)
    author: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    issue: Mapped[Issue] = relationship(back_populates="comments")


class EntityEdge(Base):
    """A typed, directed edge between two entities in the repo graph.

    Single polymorphic table — (source_type, source_id) and (target_type,
    target_id) carry the cross-domain relationship without a FK (the target
    table varies). Edge types are CHECK-constrained at the DB layer so a new
    edge type is an explicit migration, not a silent ingester bug. See ADR 0011.

    Slice 5 ingests four edge types:
      chunk  -[introduced_by]->     commit  (per-file git blame)
      commit -[part_of]->           pr      (PR merge_commit_sha + GraphQL)
      pr     -[references_issue]->  issue   (PR body / title parsing)
      issue  -[closed_by]->         pr      (inverse of references_issue when
                                             the PR resolves the issue)
    """

    __tablename__ = "entity_edge"
    __table_args__ = (
        CheckConstraint(
            "edge_type IN ('introduced_by','part_of','references_issue','closed_by')",
            name="ck_entity_edge_type",
        ),
        UniqueConstraint(
            "repo_id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "edge_type",
            name="uq_entity_edge_distinct",
        ),
        Index(
            "ix_entity_edge_out",
            "repo_id",
            "source_type",
            "source_id",
            "edge_type",
        ),
        Index(
            "ix_entity_edge_in",
            "repo_id",
            "target_type",
            "target_id",
            "edge_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[int] = mapped_column()
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[int] = mapped_column()
    edge_type: Mapped[str] = mapped_column(String(32))
    # `edge_metadata` rather than `metadata` because SQLAlchemy's DeclarativeBase
    # reserves the latter. Stores per-edge hints (e.g. blame_line, confidence).
    edge_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="entity_edges")
