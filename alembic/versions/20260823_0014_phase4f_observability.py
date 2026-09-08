"""Phase 4F replay dispositions and calibration-only evidence."""

import sqlalchemy as sa
from alembic import op

from kalshi_predictor.data.schema import Base

revision = "20260823_0014"
down_revision = "20260823_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("replay_dispositions", "calibration_observations"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)
    with op.batch_alter_table("research_checkpoints") as batch:
        batch.add_column(
            sa.Column("disposition_counts_json", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("research_checkpoints") as batch:
        batch.drop_column("disposition_counts_json")
    bind = op.get_bind()
    for name in ("calibration_observations", "replay_dispositions"):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
