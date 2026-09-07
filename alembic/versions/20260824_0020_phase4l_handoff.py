"""Phase 4L committed snapshot-cycle research handoffs."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260824_0020"
down_revision = "20260824_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("prospective_snapshot_handoffs", "prospective_handoff_counters"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("prospective_handoff_counters", "prospective_snapshot_handoffs"):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
