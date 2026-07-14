"""Phase 3S reinforcement learning research tables."""

from alembic import op
from kalshi_predictor.data.schema import Base

revision = "20260623_0007"
down_revision = "20260623_0006"
branch_labels = None
depends_on = None

TABLES = (
    "rl_run",
    "rl_dataset_manifest",
    "rl_reward_definition",
    "rl_reward_ledger",
    "rl_behavior_policy",
    "rl_behavior_decision",
    "rl_policy_artifact",
    "rl_policy_evaluation",
    "rl_policy_segment_metric",
    "rl_policy_decision",
    "rl_policy_promotion",
    "rl_policy_rollback",
    "rl_drift_snapshot",
    "rl_holdout_access_log",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
