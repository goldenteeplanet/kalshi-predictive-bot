"""Market leg parser and link coverage table."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260624_0011"
down_revision = "20260623_0010"
branch_labels = None
depends_on = None

TABLES = ("market_legs",)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
