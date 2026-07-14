from datetime import UTC, datetime
from decimal import Decimal

from kalshi_predictor.forecasting.base import ForecastInput
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster


def test_market_implied_prefers_orderbook_midpoint() -> None:
    forecaster = MarketImpliedForecaster()
    forecast = forecaster.forecast(
        ForecastInput(
            ticker="TEST",
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            market_json={"ticker": "TEST", "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.20"},
            orderbook_json={
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "1"]],
                    "no_dollars": [["0.55", "1"]],
                }
            },
        )
    )

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.425")
    assert forecast.feature_json["source"] == "orderbook_midpoint"


def test_market_implied_falls_back_to_market_quote_midpoint() -> None:
    forecaster = MarketImpliedForecaster()
    forecast = forecaster.forecast(
        ForecastInput(
            ticker="TEST",
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            market_json={"ticker": "TEST", "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.50"},
        )
    )

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.40")
    assert forecast.feature_json["source"] == "market_quote_midpoint"


def test_market_implied_falls_back_to_last_price() -> None:
    forecaster = MarketImpliedForecaster()
    forecast = forecaster.forecast(
        ForecastInput(
            ticker="TEST",
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            market_json={"ticker": "TEST", "last_price_dollars": "0.71"},
        )
    )

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.71")
    assert forecast.market_mid_probability is None
    assert forecast.feature_json["source"] == "last_price"


def test_market_implied_skips_when_no_price_available() -> None:
    forecaster = MarketImpliedForecaster()

    assert (
        forecaster.forecast(
            ForecastInput(
                ticker="TEST",
                captured_at=datetime(2026, 1, 1, tzinfo=UTC),
                market_json={"ticker": "TEST"},
            )
        )
        is None
    )

