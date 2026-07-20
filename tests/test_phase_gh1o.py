from decimal import Decimal

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1o import evaluate_independent_forecast
from kalshi_predictor.utils.time import utc_now


def test_gh1o_advances_only_positive_independent_edge_with_all_gates() -> None:
    result = evaluate_independent_forecast(
        forecast={"ticker": "KXBTC-TEST", "model_name": "crypto_v2",
                  "forecasted_at": utc_now().isoformat(), "yes_probability": "0.75"},
        market={"volume_fp": "1000", "open_interest_fp": "500", "liquidity_dollars": "10000"},
        orderbook={"orderbook_fp": {"yes_dollars": [["0.58", "100"]], "no_dollars": [["0.40", "100"]]}},
        settings=Settings(), max_forecast_age_minutes=Decimal("120"),
    )
    assert result["executable_edge"] == "0.15"
    assert result["advance"] is True
