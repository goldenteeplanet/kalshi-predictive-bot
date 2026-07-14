"""Phase 3S offline reinforcement-learning research layer."""

from kalshi_predictor.reinforcement_learning.engine import run_rl_evaluation
from kalshi_predictor.reinforcement_learning.reports import generate_rl_policy_report

__all__ = ["generate_rl_policy_report", "run_rl_evaluation"]
