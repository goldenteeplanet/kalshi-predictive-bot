"""Phase 3O market memory tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260618_0002"
down_revision = "20260618_0001"
branch_labels = None
depends_on = None

TABLES = (
    "market_memory",
    "forecast_memory",
    "trade_memory",
    "memory_event_quarantine",
    "memory_archive_manifests",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
