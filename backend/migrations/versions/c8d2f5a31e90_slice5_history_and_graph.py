"""slice5_history_and_graph

Revision ID: c8d2f5a31e90
Revises: b1f4c2a09d31
Create Date: 2026-06-21 00:00:00.000000

Slice 5a schema:

1. New domain tables: commit, pull_request, pr_comment, issue, issue_comment
   (GitHub history mirrored into the local DB so retrieval and graph traversal
   never depend on a live GitHub call). See ADR 0011.

2. New `entity_edge` table — a single polymorphic edges table connecting
   chunks / commits / PRs / issues. CHECK on edge_type, UNIQUE on the full
   (source, target, type) tuple, outbound and inbound traversal indexes.
   See ADR 0011 for why polymorphic-single-table.

3. Rename chunk_embedding -> entity_embedding (polymorphic). Slice 3's table
   was FK-bound to code_chunk; Slice 5 widens it to embed commits / PRs /
   issues too. chunk_id -> entity_id, add entity_type ('chunk' by default —
   backfills existing rows). Drop the FK to code_chunk (type now varies).
   See ADR 0013.

4. Repo.history_ingestion_status / _progress / _state — the lifecycle fields
   that mirror Slice 3's embedding_status pattern for the upcoming
   POST /repos/{repo_id}/ingest-history background job.

The HNSW index on the old chunk_embedding table is dropped here; the embed
orchestrator will recreate it as entity_embedding_hnsw_cos on first use.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c8d2f5a31e90"
down_revision: str | Sequence[str] | None = "b1f4c2a09d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- 1. New domain tables (commits / PRs / issues + comments) ----------

    op.create_table(
        "commit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("sha", sa.String(length=40), nullable=False),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("author_email", sa.String(length=255), nullable=True),
        sa.Column("authored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "parent_shas",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("files_changed_count", sa.Integer(), nullable=True),
        sa.Column("additions", sa.Integer(), nullable=True),
        sa.Column("deletions", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repo_id", "sha", name="uq_commit_repo_sha"),
    )
    op.create_index("ix_commit_repo_id", "commit", ["repo_id"])
    op.create_index("ix_commit_repo_authored", "commit", ["repo_id", "authored_at"])

    op.create_table(
        "pull_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_commit_sha", sa.String(length=40), nullable=True),
        sa.Column("base_branch", sa.String(length=255), nullable=True),
        sa.Column("head_branch", sa.String(length=255), nullable=True),
        sa.Column("additions", sa.Integer(), nullable=True),
        sa.Column("deletions", sa.Integer(), nullable=True),
        sa.Column("files_changed_count", sa.Integer(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repo_id", "number", name="uq_pr_repo_number"),
    )
    op.create_index("ix_pull_request_repo_id", "pull_request", ["repo_id"])
    op.create_index("ix_pr_repo_merged", "pull_request", ["repo_id", "merged_at"])

    op.create_table(
        "pr_comment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # 'issue_comment' (GitHub's general PR comment), 'review_comment' (inline),
        # 'review_body' (the top-level review submission text).
        sa.Column("comment_type", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["pr_id"], ["pull_request.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("pr_id", "github_id", name="uq_pr_comment_github"),
    )
    op.create_index("ix_pr_comment_pr_id", "pr_comment", ["pr_id"])

    op.create_table(
        "issue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "labels",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # Populated during edge construction (5c) from PR body parsing; nullable
        # because most issues aren't closed by a PR or the link is missing.
        sa.Column("closing_pr_number", sa.Integer(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repo_id", "number", name="uq_issue_repo_number"),
    )
    op.create_index("ix_issue_repo_id", "issue", ["repo_id"])
    op.create_index("ix_issue_repo_state", "issue", ["repo_id", "state"])

    op.create_table(
        "issue_comment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["issue.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("issue_id", "github_id", name="uq_issue_comment_github"),
    )
    op.create_index("ix_issue_comment_issue_id", "issue_comment", ["issue_id"])

    # --- 2. Polymorphic entity_edge graph table ----------------------------

    op.create_table(
        "entity_edge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("edge_type", sa.String(length=32), nullable=False),
        # `edge_metadata` because SQLAlchemy reserves `metadata` on Base.
        sa.Column(
            "edge_metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "edge_type IN ('introduced_by','part_of','references_issue','closed_by')",
            name="ck_entity_edge_type",
        ),
        sa.UniqueConstraint(
            "repo_id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "edge_type",
            name="uq_entity_edge_distinct",
        ),
    )
    op.create_index("ix_entity_edge_repo_id", "entity_edge", ["repo_id"])
    op.create_index(
        "ix_entity_edge_out",
        "entity_edge",
        ["repo_id", "source_type", "source_id", "edge_type"],
    )
    op.create_index(
        "ix_entity_edge_in",
        "entity_edge",
        ["repo_id", "target_type", "target_id", "edge_type"],
    )

    # --- 3. Polymorphic embedding table ------------------------------------
    # Drop the HNSW index first; the orchestrator recreates it under the new
    # table name on the next embed call.
    op.execute("DROP INDEX IF EXISTS chunk_embedding_hnsw_cos")
    op.drop_index("ix_chunk_embedding_repo_model", table_name="chunk_embedding")
    op.drop_index("ix_chunk_embedding_repo_id", table_name="chunk_embedding")
    op.drop_index("ix_chunk_embedding_chunk_id", table_name="chunk_embedding")
    op.drop_constraint("chunk_embedding_chunk_id_fkey", "chunk_embedding", type_="foreignkey")
    op.rename_table("chunk_embedding", "entity_embedding")
    op.alter_column("entity_embedding", "chunk_id", new_column_name="entity_id")
    op.add_column(
        "entity_embedding",
        sa.Column(
            "entity_type",
            sa.String(length=16),
            server_default="chunk",
            nullable=False,
        ),
    )
    op.create_index("ix_entity_embedding_repo_id", "entity_embedding", ["repo_id"])
    op.create_index("ix_entity_embedding_repo_model", "entity_embedding", ["repo_id", "model_name"])
    op.create_index(
        "ix_entity_embedding_repo_type_id",
        "entity_embedding",
        ["repo_id", "entity_type", "entity_id"],
    )

    # --- 4. Repo: history ingestion lifecycle ------------------------------

    op.add_column(
        "repo",
        sa.Column(
            "history_ingestion_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "repo",
        sa.Column(
            "history_ingestion_progress",
            sa.Float(),
            server_default="0.0",
            nullable=False,
        ),
    )
    op.add_column(
        "repo",
        sa.Column(
            "history_ingestion_state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Repo lifecycle fields
    op.drop_column("repo", "history_ingestion_state")
    op.drop_column("repo", "history_ingestion_progress")
    op.drop_column("repo", "history_ingestion_status")

    # Reverse the entity_embedding rename + polymorphic widening.
    op.execute("DROP INDEX IF EXISTS entity_embedding_hnsw_cos")
    op.drop_index("ix_entity_embedding_repo_type_id", table_name="entity_embedding")
    op.drop_index("ix_entity_embedding_repo_model", table_name="entity_embedding")
    op.drop_index("ix_entity_embedding_repo_id", table_name="entity_embedding")
    # Existing non-chunk rows would violate the re-added FK; the downgrade
    # assumes the deployment hasn't yet embedded non-chunk entities.
    op.drop_column("entity_embedding", "entity_type")
    op.alter_column("entity_embedding", "entity_id", new_column_name="chunk_id")
    op.rename_table("entity_embedding", "chunk_embedding")
    op.create_foreign_key(
        "chunk_embedding_chunk_id_fkey",
        "chunk_embedding",
        "code_chunk",
        ["chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_chunk_embedding_chunk_id", "chunk_embedding", ["chunk_id"])
    op.create_index("ix_chunk_embedding_repo_id", "chunk_embedding", ["repo_id"])
    op.create_index("ix_chunk_embedding_repo_model", "chunk_embedding", ["repo_id", "model_name"])

    # New tables — drop in reverse FK order.
    op.drop_index("ix_entity_edge_in", table_name="entity_edge")
    op.drop_index("ix_entity_edge_out", table_name="entity_edge")
    op.drop_index("ix_entity_edge_repo_id", table_name="entity_edge")
    op.drop_table("entity_edge")

    op.drop_index("ix_issue_comment_issue_id", table_name="issue_comment")
    op.drop_table("issue_comment")
    op.drop_index("ix_issue_repo_state", table_name="issue")
    op.drop_index("ix_issue_repo_id", table_name="issue")
    op.drop_table("issue")
    op.drop_index("ix_pr_comment_pr_id", table_name="pr_comment")
    op.drop_table("pr_comment")
    op.drop_index("ix_pr_repo_merged", table_name="pull_request")
    op.drop_index("ix_pull_request_repo_id", table_name="pull_request")
    op.drop_table("pull_request")
    op.drop_index("ix_commit_repo_authored", table_name="commit")
    op.drop_index("ix_commit_repo_id", table_name="commit")
    op.drop_table("commit")
