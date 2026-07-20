from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import WeatherForecast, WeatherObservation
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.weather.repository import (
    get_nearest_weather_observation,
    get_weather_forecasts,
    insert_weather_features,
    normalize_location_key,
)


@dataclass(frozen=True)
class WeatherFeatureBuildSummary:
    location_key: str
    forecasts_processed: int
    features_inserted: int


def build_weather_features(
    session: Session,
    *,
    location_key: str,
    source: str = "stored_forecasts",
    settings: Settings | None = None,
) -> WeatherFeatureBuildSummary:
    location = normalize_location_key(location_key)
    active_settings = settings or get_settings()
    forecasts = get_weather_forecasts(session, location)
    inserted = 0
    generated_at = utc_now()
    for forecast in forecasts:
        features = calculate_weather_features(forecast, generated_at=generated_at)
        if (
            active_settings.weather_v2_knyc_observation_enabled
            and location == "new_york"
        ):
            evidence = _knyc_observation_evidence(
                session,
                target_time=forecast.forecast_time,
            )
            if evidence is not None:
                features["knyc_observation_evidence"] = evidence
        insert_weather_features(
            session,
            location_key=location,
            source=source,
            generated_at=generated_at,
            target_time=forecast.forecast_time,
            features=features,
            raw_json=features,
        )
        inserted += 1
    return WeatherFeatureBuildSummary(
        location_key=location,
        forecasts_processed=len(forecasts),
        features_inserted=inserted,
    )


def _knyc_observation_evidence(
    session: Session,
    *,
    target_time: Any,
) -> dict[str, Any] | None:
    observation, offset_seconds = get_nearest_weather_observation(
        session,
        location_key="new_york",
        target_time=target_time,
        source="noaa_nws_observation_non_settlement_evidence",
        tolerance_minutes=15,
    )
    if observation is None or offset_seconds is None:
        return None
    raw = decode_json(observation.raw_json)
    if str(raw.get("station_id") or "").upper() != "KNYC":
        return None
    if raw.get("evidence_role") != "NON_SETTLEMENT_POINT_OBSERVATION":
        return None
    if str(raw.get("settlement_source") or "").lower() != "the_weather_company":
        return None
    return _weather_observation_reference(
        observation,
        target_time=target_time,
        offset_seconds=offset_seconds,
    )


def _weather_observation_reference(
    observation: WeatherObservation,
    *,
    target_time: Any,
    offset_seconds: int,
) -> dict[str, Any]:
    normalized_target = parse_datetime(target_time)
    normalized_observed_at = parse_datetime(observation.observed_at)
    return {
        "table": "weather_observations",
        "id": observation.id,
        "source": observation.source,
        "station_id": "KNYC",
        "evidence_role": "NON_SETTLEMENT_POINT_OBSERVATION",
        "settlement_source": "the_weather_company",
        "target_utc_time": (
            normalized_target.isoformat() if normalized_target is not None else None
        ),
        "observation_at": (
            normalized_observed_at.isoformat()
            if normalized_observed_at is not None
            else None
        ),
        "offset_seconds": offset_seconds,
        "observation_temperature_f": observation.temperature_f,
    }


def calculate_weather_features(
    forecast: WeatherForecast,
    *,
    generated_at: Any | None = None,
) -> dict[str, Any]:
    now = generated_at or utc_now()
    temperature = to_decimal(forecast.temperature_f)
    precipitation_probability = to_decimal(forecast.precipitation_probability)
    precipitation_inches = to_decimal(forecast.precipitation_inches)
    wind_speed = to_decimal(forecast.wind_speed_mph)
    wind_gust = to_decimal(forecast.wind_gust_mph)
    forecast_age_hours = max(
        Decimal("0"),
        Decimal(str(_hours_between(now, forecast.forecast_generated_at))),
    )
    lead_time_hours = max(
        Decimal("0"),
        Decimal(str(_hours_between(forecast.forecast_time, now))),
    )
    features = {
        "temperature_f": temperature,
        "precipitation_probability": precipitation_probability,
        "expected_precipitation_inches": precipitation_inches,
        "wind_speed_mph": wind_speed,
        "wind_gust_mph": wind_gust,
        "heat_index_f": _heat_index(temperature, to_decimal(forecast.humidity)),
        "freeze_risk_score": freeze_risk_score(temperature),
        "rain_risk_score": rain_risk_score(precipitation_probability, precipitation_inches),
        "wind_risk_score": wind_risk_score(wind_speed, wind_gust),
        "temp_anomaly_score": None,
        "weather_confidence_score": weather_confidence_score(
            forecast_age_hours,
            lead_time_hours,
        ),
        "forecast_generated_at": forecast.forecast_generated_at.isoformat(),
        "source_observation_ref": {
            "table": "weather_forecasts",
            "id": forecast.id,
            "location_key": forecast.location_key,
            "source": forecast.source,
            "forecast_generated_at": forecast.forecast_generated_at.isoformat(),
            "forecast_time": forecast.forecast_time.isoformat(),
        },
        "target_time": forecast.forecast_time.isoformat(),
        "forecast_age_hours": forecast_age_hours,
        "lead_time_hours": lead_time_hours,
        "notes": _feature_notes(
            temperature=temperature,
            precipitation_probability=precipitation_probability,
            precipitation_inches=precipitation_inches,
            wind_speed=wind_speed,
            wind_gust=wind_gust,
        ),
    }
    return _stringify_feature_values(features)


def freeze_risk_score(temperature_f: Decimal | None) -> Decimal | None:
    if temperature_f is None:
        return None
    if temperature_f <= Decimal("32"):
        return Decimal("1")
    if temperature_f >= Decimal("40"):
        return Decimal("0")
    return (Decimal("40") - temperature_f) / Decimal("8")


def rain_risk_score(
    precipitation_probability: Decimal | None,
    precipitation_inches: Decimal | None,
) -> Decimal | None:
    if precipitation_probability is None and precipitation_inches is None:
        return None
    probability_component = (precipitation_probability or Decimal("0")) / Decimal("100")
    amount_component = min((precipitation_inches or Decimal("0")) / Decimal("1"), Decimal("1"))
    return _clamp_score(
        (probability_component * Decimal("0.7")) + (amount_component * Decimal("0.3"))
    )


def wind_risk_score(
    wind_speed_mph: Decimal | None,
    wind_gust_mph: Decimal | None,
) -> Decimal | None:
    if wind_speed_mph is None and wind_gust_mph is None:
        return None
    speed_component = min((wind_speed_mph or Decimal("0")) / Decimal("40"), Decimal("1"))
    gust_component = min((wind_gust_mph or Decimal("0")) / Decimal("60"), Decimal("1"))
    return _clamp_score((speed_component * Decimal("0.45")) + (gust_component * Decimal("0.55")))


def weather_confidence_score(
    forecast_age_hours: Decimal,
    lead_time_hours: Decimal,
) -> Decimal:
    age_component = Decimal("1") - min(forecast_age_hours / Decimal("48"), Decimal("1"))
    lead_component = Decimal("1") - min(lead_time_hours / Decimal("168"), Decimal("1"))
    return _clamp_score((age_component + lead_component) / Decimal("2"))


def _heat_index(temperature_f: Decimal | None, humidity: Decimal | None) -> Decimal | None:
    if temperature_f is None or humidity is None or temperature_f < Decimal("80"):
        return None
    return temperature_f + ((humidity - Decimal("40")) * Decimal("0.1"))


def _hours_between(later: Any, earlier: Any) -> float:
    if getattr(later, "tzinfo", None) is not None and getattr(earlier, "tzinfo", None) is None:
        earlier = earlier.replace(tzinfo=later.tzinfo)
    if getattr(later, "tzinfo", None) is None and getattr(earlier, "tzinfo", None) is not None:
        later = later.replace(tzinfo=earlier.tzinfo)
    return (later - earlier).total_seconds() / 3600


def _feature_notes(**values: Decimal | None) -> list[str]:
    notes = []
    for key, value in values.items():
        if value is None:
            notes.append(f"Missing {key}.")
    if not notes:
        notes.append("All primary weather fields available.")
    notes.append("Temp anomaly score is null without seasonal baseline data.")
    return notes


def _clamp_score(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def _stringify_feature_values(features: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in features.items():
        if isinstance(value, Decimal):
            result[key] = decimal_to_str(value)
        else:
            result[key] = value
    return result
