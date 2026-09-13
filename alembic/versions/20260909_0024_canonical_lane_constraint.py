"""Reconcile the canonical lane invariant without relabeling historical evidence."""

from alembic import op

from kalshi_predictor.data.migration_compat import ensure_canonical_lane_check

revision = "20260909_0024"
down_revision = "20260825_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_canonical_lane_check(op)


def downgrade() -> None:
    raise RuntimeError("Lane invariant removal requires an explicitly reviewed recovery migration.")
