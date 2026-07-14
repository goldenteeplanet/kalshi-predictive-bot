import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.weather.repository import normalize_location_key


@dataclass(frozen=True)
class WeatherForecastPeriod:
    location_key: str
    source: str
    forecast_generated_at: datetime
    forecast_time: datetime
    latitude: Decimal | None
    longitude: Decimal | None
    temperature_f: Decimal | None
    dewpoint_f: Decimal | None
    humidity: Decimal | None
    wind_speed_mph: Decimal | None
    wind_gust_mph: Decimal | None
    precipitation_probability: Decimal | None
    precipitation_inches: Decimal | None
    short_forecast: str | None
    detailed_forecast: str | None
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class WeatherFetchResult:
    source: str
    forecasts: list[WeatherForecastPeriod]
    errors: list[str]


def fetch_noaa_hourly_forecast(
    *,
    location_key: str,
    latitude: float,
    longitude: float,
    user_agent: str,
    timeout_seconds: float = 10.0,
) -> WeatherFetchResult:
    headers = {"User-Agent": user_agent}
    try:
        with httpx.Client(timeout=timeout_seconds, headers=headers) as client:
            point_response = client.get(
                f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}"
            )
            point_response.raise_for_status()
            point_payload = point_response.json()
            properties = point_payload.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("NOAA point response missing properties.")
            forecast_url = properties.get("forecastHourly") or properties.get("forecast")
            if not forecast_url:
                raise ValueError("NOAA point response missing forecast URL.")
            forecast_response = client.get(str(forecast_url))
            forecast_response.raise_for_status()
            forecast_payload = forecast_response.json()
    except Exception as exc:
        return WeatherFetchResult(source="noaa", forecasts=[], errors=[str(exc)])

    try:
        forecasts = parse_noaa_hourly_forecast(
            location_key=location_key,
            latitude=latitude,
            longitude=longitude,
            payload=forecast_payload,
        )
    except Exception as exc:
        return WeatherFetchResult(source="noaa", forecasts=[], errors=[str(exc)])
    return WeatherFetchResult(source="noaa", forecasts=forecasts, errors=[])


def parse_noaa_hourly_forecast(
    *,
    location_key: str,
    latitude: Any,
    longitude: Any,
    payload: dict[str, Any],
    generated_at: datetime | None = None,
) -> list[WeatherForecastPeriod]:
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("NOAA forecast response missing properties.")
    periods = properties.get("periods")
    if not isinstance(periods, list):
        raise ValueError("NOAA forecast response missing periods.")

    generated = (
        generated_at
        or parse_datetime(properties.get("generatedAt") or properties.get("updateTime"))
        or utc_now()
    )
    parsed: list[WeatherForecastPeriod] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        forecast_time = parse_datetime(period.get("startTime"))
        if forecast_time is None:
            continue
        parsed.append(
            WeatherForecastPeriod(
                location_key=normalize_location_key(location_key),
                source="noaa",
                forecast_generated_at=generated,
                forecast_time=forecast_time,
                latitude=to_decimal(latitude),
                longitude=to_decimal(longitude),
                temperature_f=_temperature_to_f(
                    period.get("temperature"),
                    period.get("temperatureUnit"),
                ),
                dewpoint_f=_unit_value_to_f(period.get("dewpoint")),
                humidity=_unit_value(period.get("relativeHumidity")),
                wind_speed_mph=_speed_to_mph(period.get("windSpeed")),
                wind_gust_mph=_speed_to_mph(period.get("windGust")),
                precipitation_probability=_unit_value(
                    period.get("probabilityOfPrecipitation")
                ),
                precipitation_inches=_precipitation_inches(
                    period.get("quantitativePrecipitation")
                ),
                short_forecast=_str_or_none(period.get("shortForecast")),
                detailed_forecast=_str_or_none(period.get("detailedForecast")),
                raw_json=period,
            )
        )
    return parsed


def _temperature_to_f(value: Any, unit: Any) -> Decimal | None:
    numeric = to_decimal(value)
    if numeric is None:
        return None
    if str(unit or "").upper() == "C":
        return (numeric * Decimal("9") / Decimal("5")) + Decimal("32")
    return numeric


def _unit_value(payload: Any) -> Decimal | None:
    if isinstance(payload, dict):
        return to_decimal(payload.get("value"))
    return to_decimal(payload)


def _unit_value_to_f(payload: Any) -> Decimal | None:
    if isinstance(payload, dict):
        value = to_decimal(payload.get("value"))
        unit = str(payload.get("unitCode") or "")
        if value is None:
            return None
        if unit.endswith("degC"):
            return (value * Decimal("9") / Decimal("5")) + Decimal("32")
        return value
    return to_decimal(payload)


def _speed_to_mph(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return to_decimal(value)
    text = str(value)
    numbers = [to_decimal(match) for match in re.findall(r"\d+(?:\.\d+)?", text)]
    values = [number for number in numbers if number is not None]
    if not values:
        return None
    return max(values)


def _precipitation_inches(payload: Any) -> Decimal | None:
    value = _unit_value(payload)
    if value is None:
        return None
    unit = str(payload.get("unitCode") if isinstance(payload, dict) else "")
    if unit.endswith("mm"):
        return value / Decimal("25.4")
    return value


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
