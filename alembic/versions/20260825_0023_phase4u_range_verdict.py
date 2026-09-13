"""phase4u forward-only range comparator verdict

Revision ID: 20260825_0023
Revises: 20260825_0022
"""

import sqlalchemy as sa
from alembic import op

from kalshi_predictor.data.migration_compat import ensure_compatible_column

revision = "20260825_0023"
down_revision = "20260825_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_compatible_column(
        op,
        "prospective_paired_captures",
        sa.Column("range_comparator_verdict_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("prospective_paired_captures") as batch:
        batch.drop_column("range_comparator_verdict_json")
