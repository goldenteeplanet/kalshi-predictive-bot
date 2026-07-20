"""Exact parsing for supported Kalshi point-temperature contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_NYC_PATTERN = re.compile(
    r"^(?P<series>KXTEMPNYCH)-(?P<year>\d{2})(?P<month>[A-Z]{3})"
    r"(?P<day>\d{2})(?P<hour>\d{2})-(?P<kind>[TB])"
    r"(?P<strike>-?\d+(?:\.\d+)?)$"
)


@dataclass(frozen=True)
class PointTemperatureContract:
    ticker: str
    series_ticker: str
    location_key: str
    station_id: str
    settlement_source: str
    timezone: str
    target_local_time: datetime
    target_utc_time: datetime
    contract_kind: str
    raw_strike: Decimal
    discrete_threshold_f: Decimal | None


@dataclass(frozen=True)
class MarketMetadataValidation:
    contract: PointTemperatureContract
    passed: bool
    blockers: tuple[str, ...]
    floor_strike: Decimal | None
    cap_strike: Decimal | None


def parse_point_temperature_ticker(ticker: str) -> PointTemperatureContract | None:
    """Parse only the exact KXTEMPNYCH grammar currently verified from Kalshi.

    ``T80.99`` is a greater-than contract whose integer-temperature wording is
    "81 F or above". Bucket tickers are recognized syntactically, but no range
    is invented without authoritative floor/cap market metadata.
    """
    match = _NYC_PATTERN.fullmatch(ticker)
    if match is None:
        return None
    month = _MONTHS.get(match.group("month"))
    hour = int(match.group("hour"))
    if month is None or hour > 23:
        return None
    try:
        local_time = datetime(
            2000 + int(match.group("year")), month, int(match.group("day")), hour,
            tzinfo=ZoneInfo("America/New_York"),
        )
        strike = Decimal(match.group("strike"))
    except (ValueError, ArithmeticError):
        return None
    kind = match.group("kind")
    return PointTemperatureContract(
        ticker=ticker,
        series_ticker=match.group("series"),
        location_key="new_york",
        station_id="KNYC",
        settlement_source="the_weather_company",
        timezone="America/New_York",
        target_local_time=local_time,
        target_utc_time=local_time.astimezone(ZoneInfo("UTC")),
        contract_kind="ABOVE" if kind == "T" else "BUCKET_METADATA_REQUIRED",
        raw_strike=strike,
        discrete_threshold_f=(
            strike.to_integral_value(rounding=ROUND_CEILING) if kind == "T" else None
        ),
    )


def validate_point_temperature_market(
    contract: PointTemperatureContract,
    market: Mapping[str, Any],
    *,
    series_scope: str | None = None,
) -> MarketMetadataValidation:
    """Validate ticker-derived facts against authoritative market metadata."""
    blockers: list[str] = []
    floor = to_decimal(market.get("floor_strike"))
    cap = to_decimal(market.get("cap_strike"))
    strike_type = str(market.get("strike_type") or "").lower()
    close_time = parse_datetime(market.get("close_time"))
    rules = " ".join(
        str(market.get(key) or "")
        for key in ("rules_primary", "rules_secondary", "subtitle", "yes_sub_title")
    ).lower()

    if str(market.get("ticker") or "") != contract.ticker:
        blockers.append("TICKER_MISMATCH")
    metadata_series = str(market.get("series_ticker") or "")
    event_ticker = str(market.get("event_ticker") or "")
    series_verified = metadata_series == contract.series_ticker or (
        not metadata_series
        and series_scope == contract.series_ticker
        and event_ticker.startswith(f"{contract.series_ticker}-")
    )
    if not series_verified:
        blockers.append("SERIES_MISMATCH")
    if close_time != contract.target_utc_time:
        blockers.append("TARGET_TIME_MISMATCH")
    if "the weather company" not in rules:
        blockers.append("SETTLEMENT_SOURCE_MISMATCH")
    if contract.station_id.lower() not in rules:
        blockers.append("STATION_MISMATCH")
    if contract.contract_kind == "ABOVE":
        if strike_type != "greater":
            blockers.append("STRIKE_TYPE_MISMATCH")
        if floor != contract.raw_strike:
            blockers.append("FLOOR_STRIKE_MISMATCH")
        if cap is not None:
            blockers.append("UNEXPECTED_CAP_STRIKE")
    else:
        if strike_type != "between":
            blockers.append("STRIKE_TYPE_MISMATCH")
        if floor is None or cap is None or floor > cap:
            blockers.append("BUCKET_BOUNDS_MISSING_OR_INVALID")
        elif not (floor <= contract.raw_strike <= cap):
            blockers.append("BUCKET_TICKER_STRIKE_OUTSIDE_BOUNDS")

    return MarketMetadataValidation(
        contract=contract,
        passed=not blockers,
        blockers=tuple(blockers),
        floor_strike=floor,
        cap_strike=cap,
    )
