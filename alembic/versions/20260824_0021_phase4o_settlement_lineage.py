"""Add immutable settlement lineage to prospective evaluations.

Revision ID: 20260824_0021
Revises: 20260824_0020
"""

import sqlalchemy as sa
from alembic import op

from kalshi_predictor.data.migration_compat import ensure_compatible_column

revision = "20260824_0021"
down_revision = "20260824_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("settlement_hash", sa.String(length=64), nullable=True),
        sa.Column("settlement_updated_at", sa.DateTime(timezone=True), nullable=True),
    ):
        ensure_compatible_column(
            op, "prospective_pair_evaluations", column, allow_stronger_not_null=True
        )


def downgrade() -> None:
    with op.batch_alter_table("prospective_pair_evaluations") as batch:
        batch.drop_column("settlement_updated_at")
        batch.drop_column("settlement_hash")
