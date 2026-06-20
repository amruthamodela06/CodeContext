"""rename_chunk_embedding_leftovers

Revision ID: d3e1a47b2104
Revises: c8d2f5a31e90
Create Date: 2026-06-21 00:00:01.000000

Cosmetic cleanup after the chunk_embedding -> entity_embedding rename in
c8d2f5a31e90. Postgres doesn't auto-rename sequences, the PK index, or
non-FK-column foreign-key constraints when a table is renamed via
RENAME TABLE -- they keep their original names. Nothing breaks, but the
old names show up in psql ``\\d`` output and make the new schema look
inconsistent. This migration renames them to match.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e1a47b2104"
down_revision: str | Sequence[str] | None = "c8d2f5a31e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER SEQUENCE chunk_embedding_id_seq RENAME TO entity_embedding_id_seq")
    op.execute("ALTER INDEX chunk_embedding_pkey RENAME TO entity_embedding_pkey")
    op.execute(
        "ALTER TABLE entity_embedding "
        "RENAME CONSTRAINT chunk_embedding_repo_id_fkey "
        "TO entity_embedding_repo_id_fkey"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE entity_embedding "
        "RENAME CONSTRAINT entity_embedding_repo_id_fkey "
        "TO chunk_embedding_repo_id_fkey"
    )
    op.execute("ALTER INDEX entity_embedding_pkey RENAME TO chunk_embedding_pkey")
    op.execute("ALTER SEQUENCE entity_embedding_id_seq RENAME TO chunk_embedding_id_seq")
