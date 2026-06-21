"""slice5c_graph_lifecycle

Revision ID: f8a0c9b15e23
Revises: d3e1a47b2104
Create Date: 2026-06-21 01:00:00.000000

Slice 5c — graph build lifecycle on Repo (mirrors embedding +
history-ingestion patterns). The graph job depends on both code chunking
(Slice 2) and history ingestion (Slice 5b) being complete; running it
populates entity_edge with introduced_by / part_of / references_issue /
closed_by edges. See ADR 0012.

State is a JSONB blob the orchestrator uses to persist per-stage counts
+ any error, so the status endpoint can surface a meaningful summary.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8a0c9b15e23"
down_revision: str | Sequence[str] | None = "d3e1a47b2104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo",
        sa.Column(
            "graph_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "repo",
        sa.Column(
            "graph_progress",
            sa.Float(),
            server_default="0.0",
            nullable=False,
        ),
    )
    op.add_column(
        "repo",
        sa.Column(
            "graph_state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo", "graph_state")
    op.drop_column("repo", "graph_progress")
    op.drop_column("repo", "graph_status")
