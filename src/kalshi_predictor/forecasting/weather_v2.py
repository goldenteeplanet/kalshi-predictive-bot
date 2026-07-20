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
from kalshi_predictor.weather.observation_shadow import evaluate_knyc_observation
from kalshi_predictor.weather.temperature_contracts import (
    parse_point_temperature_ticker,
    validate_point_temperature_market,
)
from kalshi_predictor.weather.temperature_probability import (
    HIGH_TEMPERATURE_SIGMA,
    probability_above,
    probability_above_with_observed_max,
    sigma_for_lead_time,
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
        feature_json = {
            "location_key": location_key,
            "linked_location_key": link.location_key,
            "weather_metric": link.weather_metric,
            "target_operator": link.target_operator,
            "target_value": link.target_value,
            "target_time": link.target_time.isoformat() if link.target_time else None,
            "market_mid": str(market_mid),
            "weather_feature_values": _feature_values(features),
            "weather_feature_id": features.id,
            "feature_snapshot_id": features.id,
            "source_observation_ref": _feature_source_reference(features),
            "adjustment": str(adjustment),
            "final_probability": str(final_probability),
            "skip_reason": None,
        }
        notes = "weather_v2 midpoint plus bounded weather adjustment."
        if self.settings.weather_v2_knyc_observation_enabled:
            guarded_result = _guarded_knyc_temperature_probability(
                snapshot=snapshot,
                link=link,
                features=features,
                market_mid=market_mid,
                baseline_probability=final_probability,
                max_adjustment=self.settings.weather_v2_max_adjustment,
            )
            if guarded_result is not None:
                final_probability, evidence = guarded_result
                feature_json["knyc_temperature_probability"] = evidence
                feature_json["final_probability"] = str(final_probability)
                if evidence["applied"]:
                    feature_json["adjustment"] = str(final_probability - market_mid)
                    notes = (
                        "weather_v2 exact KNYC probability helper with bounded "
                        "non-settlement observation evidence."
                    )

        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=final_probability,
            market_mid_probability=market_mid,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json=feature_json,
            notes=notes,
        )


def _guarded_knyc_temperature_probability(
    *,
    snapshot: MarketSnapshot,
    link: WeatherMarketLink,
    features: WeatherFeature,
    market_mid: Decimal,
    baseline_probability: Decimal,
    max_adjustment: Decimal,
) -> tuple[Decimal, dict[str, Any]] | None:
    contract = parse_point_temperature_ticker(snapshot.ticker)
    if contract is None:
        return None
    evidence: dict[str, Any] = {
        "status": "BLOCKED",
        "applied": False,
        "blocker": None,
        "contract_kind": contract.contract_kind,
        "station_id": contract.station_id,
        "settlement_source": contract.settlement_source,
        "target_utc_time": contract.target_utc_time.isoformat(),
        "raw_strike": str(contract.raw_strike),
        "baseline_probability": str(baseline_probability),
        "thresholds_changed": False,
        "max_adjustment": str(max_adjustment),
    }

    def blocked(reason: str) -> tuple[Decimal, dict[str, Any]]:
        evidence["blocker"] = reason
        return baseline_probability, evidence

    if contract.contract_kind != "ABOVE":
        return blocked("CONTRACT_KIND_NOT_ACTIVATED")
    market_validation = validate_point_temperature_market(
        contract,
        decode_json(snapshot.raw_market_json),
        series_scope=contract.series_ticker,
    )
    if not market_validation.passed:
        evidence["market_metadata_blockers"] = list(market_validation.blockers)
        return blocked("MARKET_METADATA_NOT_VERIFIED")
    link_target = to_decimal(link.target_value)
    valid_targets = {contract.raw_strike, contract.discrete_threshold_f}
    if (
        link.location_key != contract.location_key
        or link.weather_metric != "TEMPERATURE"
        or link.target_operator not in {"ABOVE", "AT_OR_ABOVE"}
        or link_target not in valid_targets
        or parse_datetime(link.target_time) != contract.target_utc_time
    ):
        return blocked("WEATHER_LINK_NOT_EXACT")
    if parse_datetime(features.target_time) != contract.target_utc_time:
        return blocked("WEATHER_FEATURE_TARGET_MISMATCH")

    raw_features = decode_json(features.raw_json)
    forecast_generated_at = parse_datetime(raw_features.get("forecast_generated_at"))
    forecast_temperature = to_decimal(features.temperature_f)
    observation_evidence = raw_features.get("knyc_observation_evidence")
    if forecast_generated_at is None:
        return blocked("FORECAST_GENERATED_AT_MISSING")
    if forecast_temperature is None:
        return blocked("FORECAST_TEMPERATURE_MISSING")
    guard = evaluate_knyc_observation(
        baseline_probability=baseline_probability,
        raw_strike=contract.raw_strike,
        target_time=contract.target_utc_time,
        evidence=(observation_evidence if isinstance(observation_evidence, dict) else None),
        max_adjustment=max_adjustment,
        enabled=False,
    )
    evidence["observation_provenance"] = guard.provenance
    if not guard.passed:
        return blocked(guard.blocker or "KNYC_OBSERVATION_NOT_VERIFIED")

    observation_temperature = to_decimal(
        observation_evidence.get("observation_temperature_f")
    )
    if observation_temperature is None:
        return blocked("OBSERVATION_TEMPERATURE_MISSING")
    lead_time_hours = max(
        0.0,
        (contract.target_utc_time - forecast_generated_at).total_seconds() / 3600,
    )
    sigma = sigma_for_lead_time(lead_time_hours, HIGH_TEMPERATURE_SIGMA)
    forecast_only = probability_above(
        float(forecast_temperature), float(contract.raw_strike), sigma
    )
    observation_conditioned = probability_above_with_observed_max(
        float(forecast_temperature),
        float(contract.raw_strike),
        sigma,
        float(observation_temperature),
    )
    raw_probability = Decimal(str(observation_conditioned))
    bounded_delta = max(-max_adjustment, min(max_adjustment, raw_probability - market_mid))
    applied_probability = _clamp_probability(market_mid + bounded_delta)
    evidence.update(
        {
            "status": "APPLIED",
            "applied": True,
            "blocker": None,
            "forecast_temperature_f": str(forecast_temperature),
            "observation_temperature_f": str(observation_temperature),
            "forecast_generated_at": forecast_generated_at.isoformat(),
            "lead_time_hours": str(lead_time_hours),
            "sigma": str(sigma),
            "forecast_only_probability": str(forecast_only),
            "observation_conditioned_probability": str(observation_conditioned),
            "bounded_adjustment": str(bounded_delta),
            "applied_probability": str(applied_probability),
            "helpers": [
                "sigma_for_lead_time",
                "probability_above",
                "probability_above_with_observed_max",
            ],
        }
    )
    return applied_probability, evidence


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


def _feature_source_reference(features: WeatherFeature) -> dict[str, Any] | None:
    raw = decode_json(features.raw_json)
    reference = raw.get("source_observation_ref")
    return dict(reference) if isinstance(reference, dict) else None


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
