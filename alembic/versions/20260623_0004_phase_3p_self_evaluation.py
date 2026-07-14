"""Phase 3P self-evaluation journal tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0004"
down_revision = "20260623_0003"
branch_labels = None
depends_on = None

TABLES = (
    "self_evaluation_runs",
    "self_evaluation_metrics",
    "self_evaluation_findings",
    "self_evaluation_journals",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
