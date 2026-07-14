"""Phase 3Q auto feature discovery research subsystem."""

from kalshi_predictor.feature_discovery.engine import run_feature_discovery
from kalshi_predictor.feature_discovery.reports import generate_feature_discovery_report

__all__ = ["generate_feature_discovery_report", "run_feature_discovery"]
