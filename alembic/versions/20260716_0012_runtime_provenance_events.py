"""Local-only append-only runtime provenance event table."""

from alembic import op

from kalshi_predictor.data.schema import Base


revision = "20260716_0012"
down_revision = "20260624_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    upgrade_bind(op.get_bind())


def upgrade_bind(bind) -> None:
    Base.metadata.tables["runtime_provenance_events"].create(
        bind=bind, checkfirst=True
    )


def downgrade() -> None:
    downgrade_bind(op.get_bind())


def downgrade_bind(bind) -> None:
    Base.metadata.tables["runtime_provenance_events"].drop(
        bind=bind, checkfirst=True
    )
