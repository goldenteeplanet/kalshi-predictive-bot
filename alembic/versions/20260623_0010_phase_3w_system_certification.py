"""Phase 3W system certification tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0010"
down_revision = "20260623_0009"
branch_labels = None
depends_on = None

TABLES = (
    "system_certification_run",
    "system_certification_artifact",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)

