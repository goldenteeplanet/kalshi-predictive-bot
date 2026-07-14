from decimal import Decimal
from typing import Any

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.ui.market_display import format_edge_cents, format_probability
from kalshi_predictor.utils.decimals import to_decimal


def primary_driver(
    ranking: Any | None,
    *,
    forecast: Any | None = None,
) -> str:
    if ranking is None:
        return "No ranked opportunity yet."
    model_name = str(_field(ranking, "forecast_model") or _field(forecast, "model_name") or "model")
    edge = format_edge_cents(_field(ranking, "estimated_edge"))
    if model_name == "crypto_v2":
        return f"Crypto momentum signal with {edge} model edge."
    if model_name == "weather_v2":
        return f"Weather feature signal with {edge} model edge."
    if model_name == "ensemble_v2":
        return f"Ensemble agreement with {edge} model edge."
    if model_name == "economic_v1":
        return f"Economic feature signal with {edge} model edge."
    return f"Forecast divergence with {edge} model edge."


def supporting_signals(
    ranking: Any | None,
    *,
    forecast: Any | None = None,
    snapshot: Any | None = None,
) -> list[str]:
    if ranking is None:
        return ["Run collection, forecasting, and opportunity scanning first."]
    signals = [
        f"{_field(ranking, 'forecast_model') or 'model'} forecast is "
        f"{format_probability(_field(ranking, 'forecast_probability'))}.",
        f"Market appears underpriced by {format_edge_cents(_field(ranking, 'estimated_edge'))}.",
    ]
    spread = to_decimal(_field(ranking, "spread"))
    if spread is not None:
        label = "acceptable" if spread <= Decimal("0.10") else "wide"
        signals.append(f"Spread is {label} at {format_edge_cents(spread)}.")
    liquidity = _field(ranking, "liquidity")
    if liquidity not in (None, "", "0"):
        signals.append(f"Liquidity context is {liquidity}.")
    feature_signal = _feature_signal(forecast)
    if feature_signal:
        signals.append(feature_signal)
    if snapshot is not None:
        signals.append("Latest local snapshot is available for review.")
    return signals[:5]


def _feature_signal(forecast: Any | None) -> str | None:
    raw = _field(forecast, "feature_json")
    features = decode_json(raw)
    if not isinstance(features, dict):
        return None
    if "component_forecasts" in features or "component_model_probabilities" in features:
        return "ensemble_v2 has component model support."
    if "momentum_score" in features:
        return f"Crypto momentum score is {features['momentum_score']}."
    if "weather_confidence_score" in features:
        return f"Weather confidence score is {features['weather_confidence_score']}."
    return None


def _field(row: Any | None, name: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)
