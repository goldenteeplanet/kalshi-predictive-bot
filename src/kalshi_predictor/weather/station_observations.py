"""Exact station and local-date scoped NWS observation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime
from kalshi_predictor.weather.temperature_contracts import MarketMetadataValidation


@dataclass(frozen=True)
class StationObservation:
    station_id: str
    source: str
    observed_at: datetime
    local_date: date
    temperature_f: Decimal
    raw_json: Mapping[str, Any]


@dataclass(frozen=True)
class PointObservationAlignment:
    validation: MarketMetadataValidation
    observation: StationObservation | None
    passed: bool
    blocker: str | None
    offset_seconds: int | None


def fetch_nws_station_observations(
    *,
    station_id: str,
    target_local_date: date,
    timezone: str,
    user_agent: str,
    timeout_seconds: float = 10.0,
    client: httpx.Client | None = None,
) -> list[StationObservation]:
    """Fetch one exact station's observations for one local calendar day."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(target_local_date, time.min, zone).astimezone(ZoneInfo("UTC"))
    end = datetime.combine(target_local_date, time.max, zone).astimezone(ZoneInfo("UTC"))
    owned_client = client is None
    active_client = client or httpx.Client(
        base_url="https://api.weather.gov", timeout=timeout_seconds,
        headers={"User-Agent": user_agent},
    )
    try:
        response = active_client.get(
            f"/stations/{station_id.upper()}/observations",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        response.raise_for_status()
        return parse_nws_station_observations(
            response.json(), station_id=station_id, target_local_date=target_local_date,
            timezone=timezone,
        )
    finally:
        if owned_client:
            active_client.close()


def parse_nws_station_observations(
    payload: Mapping[str, Any],
    *,
    station_id: str,
    target_local_date: date,
    timezone: str,
) -> list[StationObservation]:
    """Reject records that do not match both the station and local date."""
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError("NWS observation response missing features")
    expected_station = station_id.upper()
    zone = ZoneInfo(timezone)
    parsed: list[StationObservation] = []
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            continue
        actual_station = _station_from_feature(feature, properties)
        if actual_station != expected_station:
            continue
        observed_at = parse_datetime(properties.get("timestamp"))
        temperature_f = _celsius_measure_to_f(properties.get("temperature"))
        if observed_at is None or temperature_f is None:
            continue
        local_date = observed_at.astimezone(zone).date()
        if local_date != target_local_date:
            continue
        parsed.append(
            StationObservation(
                station_id=expected_station,
                source="noaa_nws_observation_non_settlement_evidence",
                observed_at=observed_at,
                local_date=local_date,
                temperature_f=temperature_f,
                raw_json=feature,
            )
        )
    return sorted(parsed, key=lambda item: item.observed_at)


def align_point_observation(
    validation: MarketMetadataValidation,
    observations: list[StationObservation],
    *,
    tolerance_minutes: int = 15,
) -> PointObservationAlignment:
    """Select the nearest exact-station observation within a strict time bound."""
    if tolerance_minutes < 0:
        raise ValueError("tolerance_minutes must be non-negative")
    if not validation.passed:
        return PointObservationAlignment(
            validation, None, False, "MARKET_METADATA_NOT_VERIFIED", None
        )
    contract = validation.contract
    eligible: list[tuple[int, datetime, StationObservation]] = []
    for observation in observations:
        if observation.station_id != contract.station_id:
            continue
        offset = int(abs((observation.observed_at - contract.target_utc_time).total_seconds()))
        if offset <= tolerance_minutes * 60:
            eligible.append((offset, observation.observed_at, observation))
    if not eligible:
        return PointObservationAlignment(
            validation, None, False, "NO_EXACT_POINT_OBSERVATION_WITHIN_TOLERANCE", None
        )
    offset, _, observation = min(eligible, key=lambda item: (item[0], item[1]))
    return PointObservationAlignment(validation, observation, True, None, offset)


def _station_from_feature(
    feature: Mapping[str, Any], properties: Mapping[str, Any]
) -> str | None:
    station = properties.get("station") or feature.get("station")
    if not station:
        return None
    return str(station).rstrip("/").rsplit("/", 1)[-1].upper()


def _celsius_measure_to_f(payload: Any) -> Decimal | None:
    if not isinstance(payload, Mapping):
        return None
    value = to_decimal(payload.get("value"))
    if value is None:
        return None
    unit = str(payload.get("unitCode") or "")
    if unit.endswith("degC"):
        return (value * Decimal("9") / Decimal("5")) + Decimal("32")
    if unit.endswith("degF"):
        return value
    return None
