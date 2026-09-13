"""Canonical Phase 4C/4D research and shadow evidence tables."""

from alembic import op

from kalshi_predictor.data.schema import Base

revision = "20260823_0013"
down_revision = "20260716_0012"
branch_labels = None
depends_on = None

TABLES = (
    "canonical_evaluations",
    "research_runs",
    "research_checkpoints",
    "research_partitions",
    "shadow_decisions",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
