"""Phase 3R synthetic markets research subsystem."""

from kalshi_predictor.synthetic_markets.engine import run_synthetic_markets
from kalshi_predictor.synthetic_markets.reports import generate_synthetic_markets_report

__all__ = ["generate_synthetic_markets_report", "run_synthetic_markets"]
