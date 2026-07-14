from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.features.repository import (
    latest_feature_snapshot_for_ticker,
    snapshot_external_payload,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.utils.decimals import clamp_probability, to_decimal

CRYPTO_TERMS = ("btc", "eth", "crypto", "bitcoin", "ethereum")


class CryptoV1Forecaster:
    model_name = "crypto_v1"

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        if not _is_crypto_market(snapshot):
            return None

        feature_snapshot = latest_feature_snapshot_for_ticker(session, snapshot.ticker)
        external_features = snapshot_external_payload(feature_snapshot)
        crypto_features = external_features.get("crypto")
        if not isinstance(crypto_features, dict):
            return None

        probability = _explicit_probability(crypto_features)
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
                "source": "crypto_features",
                "features_used": crypto_features,
                "feature_snapshot_id": feature_snapshot.id if feature_snapshot else None,
            },
            notes="Uses externally supplied crypto probability features only.",
        )


def _is_crypto_market(snapshot: MarketSnapshot) -> bool:
    raw_market = decode_json(snapshot.raw_market_json)
    text = " ".join(
        str(raw_market.get(key) or "")
        for key in ("title", "subtitle", "series_ticker", "event_ticker", "ticker")
    ).lower()
    return any(term in text for term in CRYPTO_TERMS)


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

