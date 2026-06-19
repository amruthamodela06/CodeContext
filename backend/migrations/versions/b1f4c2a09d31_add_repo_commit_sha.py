"""add_repo_commit_sha

Revision ID: b1f4c2a09d31
Revises: cec6465a6ba9
Create Date: 2026-05-30 00:00:00.000000

Adds the HEAD commit SHA captured at ingestion time, used to pin citation
permalinks to an immutable blob ref (PRD §9.4 / ADR 0010). Nullable: repos
ingested before this migration have no SHA and fall back to the branch ref.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f4c2a09d31"
down_revision: str | Sequence[str] | None = "cec6465a6ba9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("repo", sa.Column("commit_sha", sa.String(length=40), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("repo", "commit_sha")
