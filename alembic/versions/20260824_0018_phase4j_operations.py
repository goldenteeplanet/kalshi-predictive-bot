"""Phase 4J prospective capture operations."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260824_0018"
down_revision = "20260824_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in (
        "prospective_capture_leases",
        "prospective_capture_alerts",
        "prospective_health_snapshots",
    ):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in (
        "prospective_health_snapshots",
        "prospective_capture_alerts",
        "prospective_capture_leases",
    ):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
