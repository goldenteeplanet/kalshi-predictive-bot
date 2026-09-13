"""Decode public canonical Miami index captures without inventing availability.

No transport, persistence, settlement certification or model probability. Point
configuration comes from the effective timeline, not the response's latest label.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

STATIONS = frozenset({"KMIA1M", "KOPF1M", "KFLL1M", "KFXE1M", "KPMP1M"})


@dataclass(frozen=True)
class IndexPoint:
    event_at: datetime
    value_f: Decimal | None
    status: str
    contributors: int | None
    config_version: str
    station_observation_times_complete: bool
    configuration_published_by_event: bool


@dataclass(frozen=True)
class MiamiIndexCapture:
    points: tuple[IndexPoint, ...]
    index_sha256: str
    calibrations_sha256: str
    index_received_at: datetime
    calibrations_received_at: datetime
    available_at: datetime
    latest_config_version: str
    late_published_configurations: tuple[str, ...]
    index_units: str
    latest_config_matches_last_point: bool | None
    historical_public_availability: str = "UNKNOWN"

    @property
    def canonical_points(self) -> tuple[IndexPoint, ...]:
        return tuple(p for p in self.points if p.value_f is not None)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MIAMI_AWARE_RECEIPT_REQUIRED")
    return value.astimezone(UTC)


def _time(value: Any) -> datetime:
    if type(value) is not int or value < 0:
        raise ValueError("MIAMI_MILLISECOND_TIMESTAMP_REQUIRED")
    try:
        seconds, milliseconds = divmod(value, 1000)
        return datetime.fromtimestamp(seconds, UTC).replace(microsecond=milliseconds * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("MIAMI_TIMESTAMP_RANGE") from exc


def _number(value: Any) -> Decimal:
    if type(value) not in {int, Decimal}:
        raise ValueError("MIAMI_NUMERIC_VALUE_REQUIRED")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("MIAMI_FINITE_VALUE_REQUIRED")
    return number


def _load(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not 0 < len(raw) <= 4 * 1024 * 1024:
        raise ValueError("MIAMI_ORIGINAL_BYTES_BUDGET")

    def reject_constant(value: str) -> None:
        raise ValueError("MIAMI_NONFINITE_JSON")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("MIAMI_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    result = json.loads(
        raw, parse_float=Decimal, parse_constant=reject_constant, object_pairs_hook=unique_keys
    )
    if not isinstance(result, dict) or result.get("city") != "miami":
        raise ValueError("MIAMI_CITY_REQUIRED")
    return result


def decode_miami_index(
    index_raw: bytes,
    calibrations_raw: bytes,
    *,
    index_received_at: datetime,
    calibrations_received_at: datetime,
    index_units: str | None = None,
) -> MiamiIndexCapture:
    """Preserve actual acquisition clocks; event times do not prove prior access."""
    index_receipt, calibration_receipt = _aware(index_received_at), _aware(calibrations_received_at)
    index, calibration = _load(index_raw), _load(calibrations_raw)
    # The documented endpoint omits a units field. Callers must explicitly bind
    # its Fahrenheit schema rather than silently infer units from numeric values.
    if (
        index_units != "fahrenheit"
        or calibration.get("units") != "celsius"
        or ("units" in index and index["units"] != index_units)
    ):
        raise ValueError("MIAMI_UNITS_MISMATCH")
    configs = calibration.get("calibrations")
    if not isinstance(configs, list) or not 1 <= len(configs) <= 1000:
        raise ValueError("MIAMI_CONFIGURATION_BUDGET")
    timeline: list[tuple[datetime, str]] = []
    versions: set[str] = set()
    effective_times: set[datetime] = set()
    publications: dict[str, datetime] = {}
    late_publications: list[str] = []
    for config in configs:
        if not isinstance(config, dict):
            raise ValueError("MIAMI_CONFIGURATION_OBJECT_REQUIRED")
        version = config.get("config_version")
        if not isinstance(version, str) or not version or version in versions:
            raise ValueError("MIAMI_DUPLICATE_OR_MISSING_CONFIGURATION")
        effective, published = (
            _time(config.get("effective_at_ms")),
            _time(config.get("published_at_ms")),
        )
        if published > calibration_receipt or effective in effective_times:
            raise ValueError("MIAMI_CONFIGURATION_CLOCK_INVALID")
        publications[version] = published
        if published > effective:
            late_publications.append(version)
        stations = config.get("stations")
        if (
            not isinstance(stations, list)
            or len(stations) != 5
            or not all(isinstance(s, dict) for s in stations)
            or {s.get("station_id") for s in stations} != STATIONS
        ):
            raise ValueError("MIAMI_CONFIGURATION_STATIONS_INVALID")
        for station in stations:
            if _number(station.get("weight")) != Decimal("0.2"):
                raise ValueError("MIAMI_CONFIGURATION_WEIGHT_INVALID")
            _number(station.get("offset_c"))
        _number(config.get("city_reference_c"))
        versions.add(version)
        effective_times.add(effective)
        timeline.append((effective, version))
    timeline.sort()
    latest = index.get("config_version")
    if latest not in versions:
        raise ValueError("MIAMI_LATEST_CONFIGURATION_UNKNOWN")
    raw_points = index.get("timeseries")
    if not isinstance(raw_points, list) or len(raw_points) > 11000:
        raise ValueError("MIAMI_POINT_BUDGET")
    points: list[IndexPoint] = []
    previous: datetime | None = None
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise ValueError("MIAMI_POINT_OBJECT_REQUIRED")
        at = _time(raw.get("t"))
        if (
            at.second
            or at.microsecond
            or at > index_receipt
            or (previous is not None and at <= previous)
        ):
            raise ValueError("MIAMI_POINT_CLOCK_OR_ORDER_INVALID")
        previous = at
        active = [entry for entry in timeline if entry[0] <= at]
        if not active:
            raise ValueError("MIAMI_POINT_CONFIGURATION_UNCOVERED")
        status, value, contributors = raw.get("status"), raw.get("v"), raw.get("contributors")
        if status not in {"normal", "degraded", "incomplete", "unavailable"}:
            raise ValueError("MIAMI_STATUS_INVALID")
        if contributors is not None and (
            type(contributors) is not int or not 0 <= contributors <= 5
        ):
            raise ValueError("MIAMI_CONTRIBUTORS_INVALID")
        if status in {"normal", "degraded"}:
            value = _number(value)
            if not Decimal("-40") <= value <= Decimal("122") or value != value.quantize(
                Decimal(".01")
            ):
                raise ValueError("MIAMI_VALUE_RANGE_OR_PRECISION")
            if (
                contributors is None
                or contributors < 4
                or (status == "normal" and contributors != 5)
            ):
                raise ValueError("MIAMI_CANONICAL_QUORUM_INVALID")
        elif value is not None:
            raise ValueError("MIAMI_INCOMPLETE_VALUE_FORBIDDEN")
        details = raw.get("stations", [])
        if (
            not isinstance(details, list)
            or len(details) > 5
            or not all(isinstance(s, dict) for s in details)
            or len({s.get("station_id") for s in details}) != len(details)
        ):
            raise ValueError("MIAMI_DETAIL_STATIONS_INVALID")
        if status == "normal" and details and len(details) != 5:
            raise ValueError("MIAMI_NORMAL_DETAIL_QUORUM_INVALID")
        for detail in details:
            if detail.get("station_id") not in STATIONS:
                raise ValueError("MIAMI_DETAIL_STATION_UNKNOWN")
            if "received_at_ms" in detail and _time(detail["received_at_ms"]) > index_receipt:
                raise ValueError("MIAMI_STATION_RECEIPT_IN_FUTURE")
            if "obs_time_ms" in detail and _time(detail["obs_time_ms"]) > at:
                raise ValueError("MIAMI_STATION_OBSERVATION_IN_FUTURE")
            if (
                detail.get("source") == "hf_asos"
                and "obs_time_ms" in detail
                and _time(detail["obs_time_ms"]) != at
            ):
                raise ValueError("MIAMI_PRIMARY_EVENT_MINUTE_MISMATCH")
            if detail.get("temp_f") is not None:
                if not Decimal("-40") <= _number(detail["temp_f"]) <= Decimal("122"):
                    raise ValueError("MIAMI_DETAIL_TEMPERATURE_INVALID")
        points.append(
            IndexPoint(
                at,
                value,
                status,
                contributors,
                active[-1][1],
                bool(details) and all("obs_time_ms" in d for d in details),
                publications[active[-1][1]] <= at,
            )
        )
    return MiamiIndexCapture(
        tuple(points),
        hashlib.sha256(index_raw).hexdigest(),
        hashlib.sha256(calibrations_raw).hexdigest(),
        index_receipt,
        calibration_receipt,
        max(index_receipt, calibration_receipt),
        latest,
        tuple(late_publications),
        index_units,
        points[-1].config_version == latest if points else None,
    )
