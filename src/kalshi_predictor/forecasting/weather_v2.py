from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot, WeatherFeature, WeatherMarketLink
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.skip_log import log_forecast_skip
from kalshi_predictor.utils.decimals import midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.weather.repository import (
    get_latest_weather_features,
    get_latest_weather_link_for_ticker,
)


class WeatherV2Forecaster:
    model_name = "weather_v2"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        link = get_latest_weather_link_for_ticker(session, snapshot.ticker)
        if link is None:
            _skip(session, snapshot, "no weather market link", available={"snapshot": True})
            return None
        link_confidence = to_decimal(link.confidence)
        if (
            link_confidence is None
            or link_confidence < self.settings.weather_v2_min_link_confidence
        ):
            _skip(
                session,
                snapshot,
                "weather market link confidence too low",
                available={"link": True, "confidence": link.confidence},
            )
            return None

        location_key = _effective_location_key(
            link.location_key,
            self.settings.weather_v2_default_location_key,
        )
        features = get_latest_weather_features(
            session,
            location_key,
            target_time=link.target_time,
        )
        if features is None:
            _skip(
                session,
                snapshot,
                "no weather features",
                available={"link": True, "location_key": location_key},
            )
            return None
        if _forecast_age_hours(features) > self.settings.weather_v2_max_forecast_age_hours:
            _skip(
                session,
                snapshot,
                "weather features are stale",
                available={"feature_id": features.id, "location_key": location_key},
            )
            return None

        market_mid = _market_midpoint(snapshot)
        if market_mid is None:
            _skip(
                session,
                snapshot,
                "no market midpoint",
                available={
                    "best_yes_bid": snapshot.best_yes_bid,
                    "best_yes_ask": snapshot.best_yes_ask,
                    "last_price": snapshot.last_price_dollars,
                },
            )
            return None

        adjustment = _weather_adjustment(
            link=link,
            features=features,
            max_adjustment=self.settings.weather_v2_max_adjustment,
        )
        if adjustment is None:
            _skip(
                session,
                snapshot,
                "weather features do not support linked metric",
                available={"metric": link.weather_metric, "feature_id": features.id},
            )
            return None
        final_probability = _clamp_probability(market_mid + adjustment)

        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=final_probability,
            market_mid_probability=market_mid,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "location_key": location_key,
                "linked_location_key": link.location_key,
                "weather_metric": link.weather_metric,
                "target_operator": link.target_operator,
                "target_value": link.target_value,
                "target_time": link.target_time.isoformat() if link.target_time else None,
                "market_mid": str(market_mid),
                "weather_feature_values": _feature_values(features),
                "adjustment": str(adjustment),
                "final_probability": str(final_probability),
                "skip_reason": None,
            },
            notes="weather_v2 midpoint plus bounded weather adjustment.",
        )


def _weather_adjustment(
    *,
    link: WeatherMarketLink,
    features: WeatherFeature,
    max_adjustment: Decimal,
) -> Decimal | None:
    operator_direction = _operator_direction(link.target_operator)
    if operator_direction is None:
        return Decimal("0")
    if link.weather_metric == "TEMPERATURE":
        signal = _temperature_signal(link, features)
    elif link.weather_metric == "RAIN":
        signal = _risk_signal(features.rain_risk_score)
    elif link.weather_metric == "WIND":
        signal = _risk_signal(features.wind_risk_score)
    elif link.weather_metric == "FREEZE":
        signal = _risk_signal(features.freeze_risk_score)
    else:
        return None
    if signal is None:
        return None
    return _clamp_signal(signal * operator_direction) * max_adjustment


def _effective_location_key(link_location_key: str, default_location_key: str) -> str:
    if link_location_key == "unknown":
        return default_location_key
    return link_location_key


def _temperature_signal(
    link: WeatherMarketLink,
    features: WeatherFeature,
) -> Decimal | None:
    temperature = to_decimal(features.temperature_f)
    target_value = to_decimal(link.target_value)
    if temperature is None or target_value is None:
        return None
    return (temperature - target_value) / Decimal("20")


def _risk_signal(value: Any) -> Decimal | None:
    score = to_decimal(value)
    if score is None:
        return None
    return (score - Decimal("0.5")) * Decimal("2")


def _operator_direction(operator: str) -> Decimal | None:
    if operator in {"ABOVE", "AT_OR_ABOVE"}:
        return Decimal("1")
    if operator in {"BELOW", "AT_OR_BELOW"}:
        return Decimal("-1")
    if operator == "EQUALS":
        return Decimal("0")
    return None


def _forecast_age_hours(features: WeatherFeature) -> Decimal:
    raw = decode_json(features.raw_json)
    explicit_age = to_decimal(raw.get("forecast_age_hours"))
    if explicit_age is not None:
        return explicit_age
    forecast_generated_at = parse_datetime(raw.get("forecast_generated_at"))
    if forecast_generated_at is None:
        return Decimal("999")
    return Decimal(str((utc_now() - forecast_generated_at).total_seconds() / 3600))


def _feature_values(features: WeatherFeature) -> dict[str, Any]:
    return {
        "target_time": features.target_time.isoformat(),
        "temperature_f": features.temperature_f,
        "precipitation_probability": features.precipitation_probability,
        "expected_precipitation_inches": features.expected_precipitation_inches,
        "wind_speed_mph": features.wind_speed_mph,
        "wind_gust_mph": features.wind_gust_mph,
        "heat_index_f": features.heat_index_f,
        "freeze_risk_score": features.freeze_risk_score,
        "rain_risk_score": features.rain_risk_score,
        "wind_risk_score": features.wind_risk_score,
        "temp_anomaly_score": features.temp_anomaly_score,
        "weather_confidence_score": features.weather_confidence_score,
    }


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _clamp_signal(value: Decimal) -> Decimal:
    if value < Decimal("-1"):
        return Decimal("-1")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def _clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value


def _skip(
    session: Session,
    snapshot: MarketSnapshot,
    reason: str,
    *,
    available: dict[str, object],
) -> None:
    log_forecast_skip(
        session,
        model_name=WeatherV2Forecaster.model_name,
        ticker=snapshot.ticker,
        reason=reason,
        required_data=["weather market link", "weather features", "market midpoint"],
        available_data=available,
    )
