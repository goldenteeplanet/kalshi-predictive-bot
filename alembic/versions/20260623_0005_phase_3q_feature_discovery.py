"""Phase 3Q auto feature discovery research tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0005"
down_revision = "20260623_0004"
branch_labels = None
depends_on = None

TABLES = (
    "feature_discovery_run",
    "feature_candidate",
    "feature_evaluation",
    "feature_fold_result",
    "feature_segment_result",
    "feature_relationship",
    "feature_recommendation",
    "feature_holdout_access",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
