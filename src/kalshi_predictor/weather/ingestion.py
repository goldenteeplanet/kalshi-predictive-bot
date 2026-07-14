from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import get_settings
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.weather.providers import (
    WeatherFetchResult,
    WeatherForecastPeriod,
    fetch_noaa_hourly_forecast,
    parse_noaa_hourly_forecast,
)
from kalshi_predictor.weather.repository import (
    insert_weather_forecast,
    insert_weather_observation,
    normalize_location_key,
)


@dataclass(frozen=True)
class WeatherIngestionSummary:
    source: str
    forecasts_inserted: int
    observations_inserted: int
    errors: list[str]


def ingest_weather_location(
    session: Session,
    *,
    location_key: str,
    latitude: float,
    longitude: float,
    source: str = "noaa",
) -> WeatherIngestionSummary:
    if source != "noaa":
        return WeatherIngestionSummary(
            source=source,
            forecasts_inserted=0,
            observations_inserted=0,
            errors=[f"Unsupported weather source: {source}"],
        )
    settings = get_settings()
    result = fetch_noaa_hourly_forecast(
        location_key=location_key,
        latitude=latitude,
        longitude=longitude,
        user_agent=settings.kalshi_user_agent,
        timeout_seconds=settings.kalshi_request_timeout_seconds,
    )
    return store_weather_fetch_result(session, result)


def store_weather_fetch_result(
    session: Session,
    result: WeatherFetchResult,
) -> WeatherIngestionSummary:
    count = 0
    for forecast in result.forecasts:
        _insert_forecast_period(session, forecast)
        count += 1
    return WeatherIngestionSummary(
        source=result.source,
        forecasts_inserted=count,
        observations_inserted=0,
        errors=result.errors,
    )


def ingest_manual_weather_json(
    session: Session,
    payload: Mapping[str, Any],
    *,
    location_key: str | None = None,
    source: str = "manual",
) -> WeatherIngestionSummary:
    location = normalize_location_key(str(location_key or payload.get("location_key") or source))
    errors: list[str] = []
    forecasts_inserted = 0
    observations_inserted = 0

    try:
        noaa_periods = _manual_noaa_periods(payload, location)
    except Exception as exc:
        errors.append(str(exc))
        noaa_periods = []
    for period in noaa_periods:
        _insert_forecast_period(session, period)
        forecasts_inserted += 1

    if not noaa_periods:
        for record in _extract_records(payload, "forecasts", "periods"):
            forecast_time = parse_datetime(
                record.get("forecast_time") or record.get("startTime") or record.get("time")
            )
            if forecast_time is None:
                errors.append(f"Skipped forecast with missing forecast_time: {record}")
                continue
            insert_weather_forecast(
                session,
                location_key=str(record.get("location_key") or location),
                source=str(record.get("source") or payload.get("source") or source),
                forecast_generated_at=parse_datetime(
                    record.get("forecast_generated_at")
                    or record.get("generated_at")
                    or payload.get("forecast_generated_at")
                )
                or utc_now(),
                forecast_time=forecast_time,
                latitude=record.get("latitude") or payload.get("latitude"),
                longitude=record.get("longitude") or payload.get("longitude"),
                temperature_f=record.get("temperature_f") or record.get("temperature"),
                dewpoint_f=record.get("dewpoint_f") or record.get("dewpoint"),
                humidity=record.get("humidity"),
                wind_speed_mph=record.get("wind_speed_mph") or record.get("windSpeed"),
                wind_gust_mph=record.get("wind_gust_mph") or record.get("windGust"),
                precipitation_probability=record.get("precipitation_probability"),
                precipitation_inches=record.get("precipitation_inches"),
                short_forecast=record.get("short_forecast") or record.get("shortForecast"),
                detailed_forecast=record.get("detailed_forecast")
                or record.get("detailedForecast"),
                raw_json=dict(record),
            )
            forecasts_inserted += 1

    for record in _extract_records(payload, "observations"):
        observed_at = parse_datetime(record.get("observed_at") or record.get("timestamp"))
        if observed_at is None:
            errors.append(f"Skipped observation with missing observed_at: {record}")
            continue
        insert_weather_observation(
            session,
            location_key=str(record.get("location_key") or location),
            source=str(record.get("source") or payload.get("source") or source),
            observed_at=observed_at,
            latitude=record.get("latitude") or payload.get("latitude"),
            longitude=record.get("longitude") or payload.get("longitude"),
            temperature_f=record.get("temperature_f"),
            dewpoint_f=record.get("dewpoint_f"),
            humidity=record.get("humidity"),
            wind_speed_mph=record.get("wind_speed_mph"),
            wind_gust_mph=record.get("wind_gust_mph"),
            precipitation_inches=record.get("precipitation_inches"),
            raw_json=dict(record),
        )
        observations_inserted += 1

    return WeatherIngestionSummary(
        source=source,
        forecasts_inserted=forecasts_inserted,
        observations_inserted=observations_inserted,
        errors=errors,
    )


def _insert_forecast_period(session: Session, forecast: WeatherForecastPeriod) -> None:
    insert_weather_forecast(
        session,
        location_key=forecast.location_key,
        source=forecast.source,
        forecast_generated_at=forecast.forecast_generated_at,
        forecast_time=forecast.forecast_time,
        latitude=forecast.latitude,
        longitude=forecast.longitude,
        temperature_f=forecast.temperature_f,
        dewpoint_f=forecast.dewpoint_f,
        humidity=forecast.humidity,
        wind_speed_mph=forecast.wind_speed_mph,
        wind_gust_mph=forecast.wind_gust_mph,
        precipitation_probability=forecast.precipitation_probability,
        precipitation_inches=forecast.precipitation_inches,
        short_forecast=forecast.short_forecast,
        detailed_forecast=forecast.detailed_forecast,
        raw_json=forecast.raw_json,
    )


def _manual_noaa_periods(
    payload: Mapping[str, Any],
    location_key: str,
) -> list[WeatherForecastPeriod]:
    if not isinstance(payload.get("properties"), Mapping):
        return []
    return parse_noaa_hourly_forecast(
        location_key=location_key,
        latitude=payload.get("latitude") or payload.get("lat"),
        longitude=payload.get("longitude") or payload.get("lon"),
        payload=dict(payload),
        generated_at=parse_datetime(payload.get("forecast_generated_at")),
    )


def _extract_records(payload: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    if any(key in payload for key in ("forecast_time", "startTime", "observed_at")):
        return [payload]
    return []


def _decimal_or_none(value: Any):
    return to_decimal(value)
