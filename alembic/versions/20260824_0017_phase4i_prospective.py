"""Phase 4I strict-causality prospective paired research lane."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260824_0017"
down_revision = "20260824_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in (
        "prospective_capture_runs",
        "prospective_paired_captures",
        "prospective_capture_rejections",
        "prospective_pair_evaluations",
    ):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in (
        "prospective_pair_evaluations",
        "prospective_capture_rejections",
        "prospective_paired_captures",
        "prospective_capture_runs",
    ):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
