from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1m import attribute_ranking


def test_gh1m_attributes_low_edge_and_score_without_threshold_changes() -> None:
    row = {"ticker": "KXBTC-TEST", "best_side": "BUY_YES", "best_price": "0.50",
           "forecast_probability": "0.51", "estimated_edge": "0.01",
           "liquidity_score": "50", "spread": "0.02", "spread_score": "80",
           "model_confidence_score": "50", "time_score": "80", "opportunity_score": "35"}
    result = attribute_ranking(row, settings=Settings())
    assert result["first_blocker"] == "EDGE_BELOW_MINIMUM"
    assert result["opportunity_gate_pass"] is False
