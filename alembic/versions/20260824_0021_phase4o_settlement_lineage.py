"""Add immutable settlement lineage to prospective evaluations.

Revision ID: 20260824_0021
Revises: 20260824_0020
"""

import sqlalchemy as sa

from alembic import op

revision = "20260824_0021"
down_revision = "20260824_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prospective_pair_evaluations") as batch:
        batch.add_column(sa.Column("settlement_hash", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("settlement_updated_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("prospective_pair_evaluations") as batch:
        batch.drop_column("settlement_updated_at")
        batch.drop_column("settlement_hash")
