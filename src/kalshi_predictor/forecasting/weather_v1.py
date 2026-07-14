from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.features.repository import (
    latest_feature_snapshot_for_ticker,
    snapshot_external_payload,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.utils.decimals import clamp_probability, to_decimal


class WeatherV1Forecaster:
    model_name = "weather_v1"

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        feature_snapshot = latest_feature_snapshot_for_ticker(session, snapshot.ticker)
        external_features = snapshot_external_payload(feature_snapshot)
        weather_features = external_features.get("weather")
        if not isinstance(weather_features, dict):
            return None

        probability = _explicit_probability(weather_features)
        if probability is None:
            return None

        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=clamp_probability(probability),
            market_mid_probability=None,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "source": "weather_features",
                "features_used": weather_features,
                "feature_snapshot_id": feature_snapshot.id if feature_snapshot else None,
            },
            notes="Uses externally supplied weather probability features only.",
        )


def _explicit_probability(features: dict[str, Any]) -> Decimal | None:
    for key in (
        "yes_probability",
        "probability",
        "forecast_probability",
        "model_probability",
        "predicted_yes_probability",
    ):
        probability = to_decimal(features.get(key))
        if probability is not None:
            return probability
    return None

