"""Phase 4K immutable status-lineage evidence."""

from alembic import op

from kalshi_predictor.data.schema import Base

revision = "20260824_0019"
down_revision = "20260824_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["prospective_status_lineage"].create(
        bind=op.get_bind(), checkfirst=True
    )


def downgrade() -> None:
    Base.metadata.tables["prospective_status_lineage"].drop(
        bind=op.get_bind(), checkfirst=True
    )
