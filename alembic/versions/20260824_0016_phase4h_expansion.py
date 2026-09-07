"""Phase 4H checkpointed expansion and executable edge attribution."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260824_0016"
down_revision = "20260824_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in (
        "evidence_expansion_partitions",
        "evidence_expansion_members",
        "executable_edge_attributions",
    ):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in (
        "executable_edge_attributions",
        "evidence_expansion_members",
        "evidence_expansion_partitions",
    ):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
