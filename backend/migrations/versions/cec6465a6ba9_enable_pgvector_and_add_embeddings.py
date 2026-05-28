"""enable_pgvector_and_add_embeddings

Revision ID: cec6465a6ba9
Revises: 84a52eb72940
Create Date: 2026-05-28 22:47:31.142817

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "cec6465a6ba9"
down_revision: str | Sequence[str] | None = "84a52eb72940"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The pgvector extension must exist before any table with a vector column.
    # Idempotent; the pgvector/pgvector image ships the extension available but
    # not enabled in the codecontext database (ADR 0009).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "chunk_embedding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(dim=384), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["code_chunk.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chunk_embedding_chunk_id"), "chunk_embedding", ["chunk_id"], unique=False
    )
    op.create_index(
        op.f("ix_chunk_embedding_repo_id"), "chunk_embedding", ["repo_id"], unique=False
    )
    op.create_index(
        "ix_chunk_embedding_repo_model",
        "chunk_embedding",
        ["repo_id", "model_name"],
        unique=False,
    )
    # The HNSW index is intentionally NOT created here — the embed orchestrator
    # builds it after the bulk insert completes (ADR 0009).
    op.add_column(
        "repo",
        sa.Column(
            "embedding_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "repo",
        sa.Column("embedding_progress", sa.Float(), server_default="0.0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema. Leaves the `vector` extension enabled (harmless)."""
    op.drop_column("repo", "embedding_progress")
    op.drop_column("repo", "embedding_status")
    op.drop_index("ix_chunk_embedding_repo_model", table_name="chunk_embedding")
    op.drop_index(op.f("ix_chunk_embedding_repo_id"), table_name="chunk_embedding")
    op.drop_index(op.f("ix_chunk_embedding_chunk_id"), table_name="chunk_embedding")
    op.drop_table("chunk_embedding")
