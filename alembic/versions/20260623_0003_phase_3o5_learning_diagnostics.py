"""Phase 3O.5 learning diagnostics tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0003"
down_revision = "20260618_0002"
branch_labels = None
depends_on = None

TABLES = ("learning_rejection_log",)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
