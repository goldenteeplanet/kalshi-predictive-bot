"""Phase 3R synthetic markets research tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0006"
down_revision = "20260623_0005"
branch_labels = None
depends_on = None

TABLES = (
    "synthetic_market_run",
    "synthetic_event_registry",
    "synthetic_contract_registry",
    "synthetic_listing_check",
    "synthetic_listing_match",
    "synthetic_probability_estimate",
    "synthetic_model_component",
    "synthetic_constraint_result",
    "synthetic_resolution",
    "synthetic_calibration_result",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
