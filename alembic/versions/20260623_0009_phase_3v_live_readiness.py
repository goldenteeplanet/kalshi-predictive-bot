"""Phase 3V live trading readiness review tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0009"
down_revision = "20260623_0008"
branch_labels = None
depends_on = None

TABLES = (
    "readiness_review",
    "readiness_control_result",
    "readiness_evidence_manifest",
    "readiness_decision",
    "live_readiness_certificate",
    "live_readiness_certificate_event",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)

