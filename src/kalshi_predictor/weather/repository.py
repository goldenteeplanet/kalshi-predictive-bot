from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    WeatherFeature,
    WeatherForecast,
    WeatherMarketLink,
    WeatherObservation,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import parse_datetime, utc_now


def normalize_location_key(location_key: str) -> str:
    normalized = location_key.strip().lower().replace(" ", "_").replace("-", "_")
    return "_".join(part for part in normalized.split("_") if part)


def insert_weather_observation(
    session: Session,
    *,
    location_key: str,
    source: str,
    observed_at: datetime,
    latitude: Any = None,
    longitude: Any = None,
    temperature_f: Any = None,
    dewpoint_f: Any = None,
    humidity: Any = None,
    wind_speed_mph: Any = None,
    wind_gust_mph: Any = None,
    precipitation_inches: Any = None,
    raw_json: Mapping[str, Any] | None = None,
) -> WeatherObservation:
    observation = WeatherObservation(
        location_key=normalize_location_key(location_key),
        source=source,
        observed_at=observed_at,
        latitude=decimal_to_str(latitude),
        longitude=decimal_to_str(longitude),
        temperature_f=decimal_to_str(temperature_f),
        dewpoint_f=decimal_to_str(dewpoint_f),
        humidity=decimal_to_str(humidity),
        wind_speed_mph=decimal_to_str(wind_speed_mph),
        wind_gust_mph=decimal_to_str(wind_gust_mph),
        precipitation_inches=decimal_to_str(precipitation_inches),
        raw_json=encode_json(dict(raw_json or {})),
        created_at=utc_now(),
    )
    session.add(observation)
    session.flush()
    return observation


def insert_weather_observation_if_missing(
    session: Session,
    **values: Any,
) -> tuple[WeatherObservation, bool]:
    location_key = normalize_location_key(str(values["location_key"]))
    source = str(values["source"])
    observed_at = parse_datetime(values["observed_at"])
    if observed_at is None:
        raise ValueError("observed_at is required")
    existing = session.scalar(
        select(WeatherObservation)
        .where(
            WeatherObservation.location_key == location_key,
            WeatherObservation.source == source,
            WeatherObservation.observed_at == observed_at,
        )
        .order_by(desc(WeatherObservation.id))
        .limit(1)
    )
    if existing is not None:
        return existing, False
    return (
        insert_weather_observation(
            session,
            **{**values, "location_key": location_key, "observed_at": observed_at},
        ),
        True,
    )


def get_nearest_weather_observation(
    session: Session,
    *,
    location_key: str,
    target_time: datetime,
    source: str | None = None,
    tolerance_minutes: int = 15,
) -> tuple[WeatherObservation | None, int | None]:
    if tolerance_minutes < 0:
        raise ValueError("tolerance_minutes must be non-negative")
    target = parse_datetime(target_time)
    if target is None:
        return None, None
    tolerance = timedelta(minutes=tolerance_minutes)
    statement = select(WeatherObservation).where(
        WeatherObservation.location_key == normalize_location_key(location_key),
        WeatherObservation.observed_at >= target - tolerance,
        WeatherObservation.observed_at <= target + tolerance,
    )
    if source is not None:
        statement = statement.where(WeatherObservation.source == source)
    rows = list(session.scalars(statement.order_by(WeatherObservation.observed_at)))
    candidates: list[tuple[int, datetime, int, WeatherObservation]] = []
    for row in rows:
        observed_at = parse_datetime(row.observed_at)
        if observed_at is None:
            continue
        offset = int(abs((observed_at - target).total_seconds()))
        candidates.append((offset, observed_at, int(row.id or 0), row))
    if not candidates:
        return None, None
    offset, _, _, observation = min(candidates, key=lambda item: item[:3])
    return observation, offset


def insert_weather_forecast(
    session: Session,
    *,
    location_key: str,
    source: str,
    forecast_generated_at: datetime,
    forecast_time: datetime,
    latitude: Any = None,
    longitude: Any = None,
    temperature_f: Any = None,
    dewpoint_f: Any = None,
    humidity: Any = None,
    wind_speed_mph: Any = None,
    wind_gust_mph: Any = None,
    precipitation_probability: Any = None,
    precipitation_inches: Any = None,
    short_forecast: str | None = None,
    detailed_forecast: str | None = None,
    raw_json: Mapping[str, Any] | None = None,
) -> WeatherForecast:
    forecast = WeatherForecast(
        location_key=normalize_location_key(location_key),
        source=source,
        forecast_generated_at=forecast_generated_at,
        forecast_time=forecast_time,
        latitude=decimal_to_str(latitude),
        longitude=decimal_to_str(longitude),
        temperature_f=decimal_to_str(temperature_f),
        dewpoint_f=decimal_to_str(dewpoint_f),
        humidity=decimal_to_str(humidity),
        wind_speed_mph=decimal_to_str(wind_speed_mph),
        wind_gust_mph=decimal_to_str(wind_gust_mph),
        precipitation_probability=decimal_to_str(precipitation_probability),
        precipitation_inches=decimal_to_str(precipitation_inches),
        short_forecast=short_forecast,
        detailed_forecast=detailed_forecast,
        raw_json=encode_json(dict(raw_json or {})),
        created_at=utc_now(),
    )
    session.add(forecast)
    session.flush()
    return forecast


def get_weather_forecasts(
    session: Session,
    location_key: str,
    *,
    limit: int | None = None,
) -> list[WeatherForecast]:
    statement = (
        select(WeatherForecast)
        .where(WeatherForecast.location_key == normalize_location_key(location_key))
        .order_by(WeatherForecast.forecast_time, WeatherForecast.id)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def get_latest_weather_forecasts(
    session: Session,
    location_key: str,
    *,
    limit: int = 10,
) -> list[WeatherForecast]:
    return list(
        session.scalars(
            select(WeatherForecast)
            .where(WeatherForecast.location_key == normalize_location_key(location_key))
            .order_by(desc(WeatherForecast.forecast_generated_at), WeatherForecast.forecast_time)
            .limit(limit)
        )
    )


def insert_weather_features(
    session: Session,
    *,
    location_key: str,
    source: str,
    generated_at: datetime,
    target_time: datetime,
    features: Mapping[str, Any],
    raw_json: Mapping[str, Any] | None = None,
) -> WeatherFeature:
    feature = WeatherFeature(
        location_key=normalize_location_key(location_key),
        source=source,
        generated_at=generated_at,
        target_time=target_time,
        temperature_f=decimal_to_str(features.get("temperature_f")),
        precipitation_probability=decimal_to_str(features.get("precipitation_probability")),
        expected_precipitation_inches=decimal_to_str(
            features.get("expected_precipitation_inches")
        ),
        wind_speed_mph=decimal_to_str(features.get("wind_speed_mph")),
        wind_gust_mph=decimal_to_str(features.get("wind_gust_mph")),
        heat_index_f=decimal_to_str(features.get("heat_index_f")),
        freeze_risk_score=decimal_to_str(features.get("freeze_risk_score")),
        rain_risk_score=decimal_to_str(features.get("rain_risk_score")),
        wind_risk_score=decimal_to_str(features.get("wind_risk_score")),
        temp_anomaly_score=decimal_to_str(features.get("temp_anomaly_score")),
        weather_confidence_score=decimal_to_str(features.get("weather_confidence_score")),
        raw_json=encode_json(dict(raw_json or features)),
        created_at=utc_now(),
    )
    session.add(feature)
    session.flush()
    return feature


def get_latest_weather_features(
    session: Session,
    location_key: str,
    *,
    target_time: datetime | None = None,
) -> WeatherFeature | None:
    location = normalize_location_key(location_key)
    if target_time is None:
        return session.scalar(
            select(WeatherFeature)
            .where(WeatherFeature.location_key == location)
            .order_by(desc(WeatherFeature.generated_at), WeatherFeature.target_time)
            .limit(1)
        )
    requested_target = parse_datetime(target_time)
    if requested_target is None:
        return None
    return session.scalar(
        select(WeatherFeature)
        .where(
            WeatherFeature.location_key == location,
            WeatherFeature.target_time == requested_target,
        )
        .order_by(
            desc(WeatherFeature.generated_at),
            desc(WeatherFeature.created_at),
            desc(WeatherFeature.id),
        )
        .limit(1)
    )


def _weather_feature_target_sort_key(
    feature: WeatherFeature,
    target_time: datetime,
) -> tuple[float, float, float, float, int]:
    raw = decode_json(feature.raw_json)
    forecast_generated_at = parse_datetime(raw.get("forecast_generated_at"))
    feature_target = parse_datetime(feature.target_time)
    requested_target = parse_datetime(target_time)
    if feature_target is None or requested_target is None:
        distance = float("inf")
    else:
        distance = abs((feature_target - requested_target).total_seconds())
    return (
        distance,
        -_timestamp(forecast_generated_at),
        -_timestamp(feature.generated_at),
        -_timestamp(feature.created_at),
        -int(feature.id or 0),
    )


def _timestamp(value: datetime | None) -> float:
    parsed = parse_datetime(value)
    return parsed.timestamp() if parsed is not None else 0.0


def get_weather_features(
    session: Session,
    location_key: str,
    *,
    limit: int | None = None,
) -> list[WeatherFeature]:
    statement = (
        select(WeatherFeature)
        .where(WeatherFeature.location_key == normalize_location_key(location_key))
        .order_by(desc(WeatherFeature.generated_at), WeatherFeature.target_time)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def insert_weather_market_link(
    session: Session,
    *,
    ticker: str,
    location_key: str,
    weather_metric: str,
    target_operator: str,
    confidence: Any,
    reason: str,
    target_value: Any = None,
    target_time: datetime | None = None,
    raw_json: Mapping[str, Any] | None = None,
    detected_at: datetime | None = None,
) -> WeatherMarketLink:
    link = WeatherMarketLink(
        ticker=ticker,
        location_key=normalize_location_key(location_key),
        detected_at=detected_at or utc_now(),
        weather_metric=weather_metric,
        target_operator=target_operator,
        target_value=decimal_to_str(target_value),
        target_time=target_time,
        confidence=decimal_to_str(confidence) or "0",
        reason=reason,
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(link)
    session.flush()
    return link


def get_latest_weather_link_for_ticker(
    session: Session,
    ticker: str,
) -> WeatherMarketLink | None:
    return session.scalar(
        select(WeatherMarketLink)
        .where(WeatherMarketLink.ticker == ticker)
        .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
        .limit(1)
    )


def get_weather_links(
    session: Session,
    *,
    limit: int | None = None,
) -> list[WeatherMarketLink]:
    statement = select(WeatherMarketLink).order_by(
        desc(WeatherMarketLink.detected_at),
        WeatherMarketLink.ticker,
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))
