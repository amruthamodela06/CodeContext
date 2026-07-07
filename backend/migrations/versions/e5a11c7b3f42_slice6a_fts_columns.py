"""slice6a_fts_columns

Revision ID: e5a11c7b3f42
Revises: f8a0c9b15e23
Create Date: 2026-06-25 00:00:00.000000

Slice 6a -- full-text search columns for hybrid retrieval (BM25 + vector).
See ADR 0014.

Adds:
- code_chunk: fts_name / fts_doc / fts_body TEXT (app-managed intermediates
  populated during chunking; camelCase / snake_case identifier splits live
  in fts_name) + fts_tsv tsvector GENERATED from those three with weights
  A / B / D. Backfill of existing chunks is a separate Slice 6b job.
- commit: fts_tsv GENERATED from split_part(message, '\\n', 1) (subject,
  weight A) + full message (weight B).
- pull_request: fts_tsv GENERATED from title (weight A) + body (weight B).
- issue: fts_tsv GENERATED from title (weight A) + body (weight B).

All four get GIN indexes on fts_tsv (read-heavy pattern; writes batched at
ingestion time).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a11c7b3f42"
down_revision: str | Sequence[str] | None = "f8a0c9b15e23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHUNK_FTS_EXPR = (
    "setweight(to_tsvector('english', coalesce(fts_name, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(fts_doc, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(fts_body, '')), 'D')"
)
_COMMIT_FTS_EXPR = (
    "setweight(to_tsvector('english', coalesce(split_part(message, E'\\n', 1), '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(message, '')), 'B')"
)
_PR_ISSUE_FTS_EXPR = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(body, '')), 'B')"
)


def upgrade() -> None:
    # code_chunk: three app-managed TEXT intermediates + generated tsvector.
    op.add_column(
        "code_chunk",
        sa.Column("fts_name", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "code_chunk",
        sa.Column("fts_doc", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "code_chunk",
        sa.Column("fts_body", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "code_chunk",
        sa.Column(
            "fts_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(_CHUNK_FTS_EXPR, persisted=True),
        ),
    )
    op.create_index(
        "ix_code_chunk_fts_tsv",
        "code_chunk",
        ["fts_tsv"],
        postgresql_using="gin",
    )

    # commit: subject (A) + full message (B).
    op.add_column(
        "commit",
        sa.Column(
            "fts_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(_COMMIT_FTS_EXPR, persisted=True),
        ),
    )
    op.create_index(
        "ix_commit_fts_tsv",
        "commit",
        ["fts_tsv"],
        postgresql_using="gin",
    )

    # pull_request: title (A) + body (B).
    op.add_column(
        "pull_request",
        sa.Column(
            "fts_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(_PR_ISSUE_FTS_EXPR, persisted=True),
        ),
    )
    op.create_index(
        "ix_pr_fts_tsv",
        "pull_request",
        ["fts_tsv"],
        postgresql_using="gin",
    )

    # issue: title (A) + body (B).
    op.add_column(
        "issue",
        sa.Column(
            "fts_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(_PR_ISSUE_FTS_EXPR, persisted=True),
        ),
    )
    op.create_index(
        "ix_issue_fts_tsv",
        "issue",
        ["fts_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_issue_fts_tsv", table_name="issue")
    op.drop_column("issue", "fts_tsv")

    op.drop_index("ix_pr_fts_tsv", table_name="pull_request")
    op.drop_column("pull_request", "fts_tsv")

    op.drop_index("ix_commit_fts_tsv", table_name="commit")
    op.drop_column("commit", "fts_tsv")

    op.drop_index("ix_code_chunk_fts_tsv", table_name="code_chunk")
    op.drop_column("code_chunk", "fts_tsv")
    op.drop_column("code_chunk", "fts_body")
    op.drop_column("code_chunk", "fts_doc")
    op.drop_column("code_chunk", "fts_name")
