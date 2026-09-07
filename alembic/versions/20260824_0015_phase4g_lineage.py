"""Phase 4G immutable crypto lineage and event-level calibration."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260824_0015"
down_revision = "20260823_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("crypto_feature_lineage", "event_calibration_metrics"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("event_calibration_metrics", "crypto_feature_lineage"):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
