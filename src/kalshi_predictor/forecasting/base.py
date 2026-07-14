from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class ForecastInput:
    ticker: str
    captured_at: datetime
    market_json: dict[str, Any]
    orderbook_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class ForecastOutput:
    ticker: str
    forecasted_at: datetime
    model_name: str
    yes_probability: Decimal
    market_mid_probability: Decimal | None
    best_yes_bid: Decimal | None
    best_yes_ask: Decimal | None
    feature_json: dict[str, Any]
    notes: str | None = None


class BaseForecaster(Protocol):
    model_name: str

    def forecast(self, forecast_input: ForecastInput) -> ForecastOutput | None:
        """Return a forecast or None when inputs are not usable."""

