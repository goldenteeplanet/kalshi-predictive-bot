"""Pure research bridge into existing economic feature arithmetic; no persistence.

Provider observation levels are not inflation percentages, payroll changes,
consensus surprises, or calibrated probabilities. Receipts are not release times.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from kalshi_predictor.data.schema import EconomicEvent
from kalshi_predictor.economic.features import calculate_economic_features
from kalshi_predictor.research.bls import BLSResponse
from kalshi_predictor.research.fred import FREDResponse

# Concept, units, seasonal treatment, frequency. Deliberately no market mapping.
_BLS = {
    "CUUR0000SA0": ("cpi_all_items_level", "index_1982_84_100", "NSA", "monthly"),
    "CUSR0000SA0": ("cpi_all_items_level", "index_1982_84_100", "SA", "monthly"),
    "CUSR0000SA0L1E": ("cpi_core_level", "index_1982_84_100", "SA", "monthly"),
    "LNS14000000": ("unemployment_rate", "percent", "SA", "monthly"),
    "CES0000000001": ("nonfarm_payroll_level", "thousand_persons", "SA", "monthly"),
    "CES0500000003": ("average_hourly_earnings_level", "usd_per_hour", "SA", "monthly"),
    "WPSFD4": ("ppi_final_demand_level", "index_2009_11_100", "SA", "monthly"),
}
_FRED = {
    "CPIAUCSL": _BLS["CUSR0000SA0"],
    "CPILFESL": _BLS["CUSR0000SA0L1E"],
    "UNRATE": _BLS["LNS14000000"],
    "PAYEMS": _BLS["CES0000000001"],
    "DFF": ("effective_federal_funds_rate", "percent", "NSA", "daily"),
    "GDPC1": ("real_gdp_level", "billion_chained_2017_usd_SAAR", "SA", "quarterly"),
}


class EconomicSourceError(ValueError):
    """Fixed research diagnostic."""


@dataclass(frozen=True)
class ResearchObservation:
    period: str
    observation_date: date
    raw_value: str
    numeric_value: Decimal | None
    realtime_start: str | None
    realtime_end: str | None
    footnotes_json: str


@dataclass(frozen=True)
class EconomicSourceFeatures:
    provider: str
    series_id: str
    concept: str
    units: str
    seasonal_adjustment: str
    frequency: str
    source_sha256: str
    source_url: str
    received_at: datetime
    decision_at: datetime
    observations: tuple[ResearchObservation, ...]
    momentum_score: Decimal | None
    momentum_direction: str
    momentum_status: str
    contribution: str = field(default="FEATURE_PRESENT", init=False)
    research_only: bool = field(default=True, init=False)
    runtime_certified: bool = field(default=False, init=False)
    historical_availability_verified: bool = field(default=False, init=False)
    provider_timestamp: None = None
    release_identity: None = None


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EconomicSourceError("ECON_SOURCE_CLOCK_INVALID")
    return value.astimezone(UTC)


def _day(value: Any) -> date:
    parsed = None
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            pass
    if parsed is None or parsed.isoformat() != value:
        raise EconomicSourceError("ECON_SOURCE_DATE_INVALID")
    return parsed


def _number(raw: Any) -> Decimal | None:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 100:
        raise EconomicSourceError("ECON_SOURCE_VALUE_INVALID")
    number = None
    try:
        number = Decimal(raw)
    except ArithmeticError:
        pass
    if number is None:
        # Missing symbols remain visible, and never become zero or a feature.
        return None
    if not number.is_finite() or abs(number) > Decimal("1e15"):
        raise EconomicSourceError("ECON_SOURCE_VALUE_INVALID")
    return number


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EconomicSourceError("ECON_SOURCE_DUPLICATE_JSON")
        value[key] = item
    return value


def build_economic_source_features(
    response: BLSResponse | FREDResponse,
    *,
    decision_at: datetime,
) -> EconomicSourceFeatures:
    if type(response) not in {BLSResponse, FREDResponse} or response.kind != "observations":
        raise EconomicSourceError("ECON_SOURCE_RESPONSE_UNSUPPORTED")
    receipt, decision = _aware(response.original.received_at), _aware(decision_at)
    if receipt > decision:
        raise EconomicSourceError("ECON_SOURCE_NOT_RECEIVED_AT_DECISION")
    raw = response.original.payload
    if len(raw) > 1_048_576 or hashlib.sha256(raw).hexdigest() != response.original.sha256:
        raise EconomicSourceError("ECON_SOURCE_HASH_MISMATCH")
    data = None
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        pass
    if not isinstance(data, dict) or data != response.data:
        raise EconomicSourceError("ECON_SOURCE_BODY_MISMATCH")
    provider = "BLS" if type(response) is BLSResponse else "FRED"
    mapping = _BLS if provider == "BLS" else _FRED
    if response.series_id not in mapping:
        raise EconomicSourceError("ECON_SOURCE_SERIES_UNSUPPORTED")
    concept, units, seasonal, frequency = mapping[response.series_id]
    rows: Any
    if provider == "BLS":
        if response.original.url != "https://api.bls.gov/publicAPI/v2/timeseries/data/":
            raise EconomicSourceError("ECON_SOURCE_URL_INVALID")
        if data.get("status") != "REQUEST_SUCCEEDED":
            raise EconomicSourceError("ECON_SOURCE_PROVIDER_FAILED")
        results = data.get("Results")
        if isinstance(results, list) and len(results) == 1:
            results = results[0]
        series = results.get("series") if isinstance(results, dict) else None
        if (
            not isinstance(series, list)
            or len(series) != 1
            or not isinstance(series[0], dict)
            or series[0].get("seriesID") != response.series_id
        ):
            raise EconomicSourceError("ECON_SOURCE_SERIES_MISMATCH")
        rows = series[0].get("data")
        query: dict[str, list[str]] = {}
    else:
        if data.get("units", "lin") != "lin" or data.get("output_type", 1) != 1:
            raise EconomicSourceError("ECON_SOURCE_TRANSFORM_INVALID")
        url = urlsplit(response.original.url)
        query = parse_qs(url.query)
        allowed = {
            "series_id",
            "file_type",
            "observation_start",
            "observation_end",
            "realtime_start",
            "realtime_end",
            "limit",
            "sort_order",
        }
        if (
            url.scheme != "https"
            or url.netloc != "api.stlouisfed.org"
            or url.path != "/fred/series/observations"
            or url.fragment
            or not query.keys() <= allowed
            or any(len(v) != 1 for v in query.values())
            or query.get("series_id") != [response.series_id]
        ):
            raise EconomicSourceError("ECON_SOURCE_URL_OR_TRANSFORM_INVALID")
        for key in ("realtime_start", "realtime_end", "observation_start", "observation_end"):
            if key not in query:
                raise EconomicSourceError("ECON_SOURCE_WINDOW_MISSING")
            _day(query[key][0])
        if query["realtime_start"][0] != query["realtime_end"][0]:
            raise EconomicSourceError("ECON_SOURCE_SINGLE_VINTAGE_REQUIRED")
        if _day(query["realtime_end"][0]) > decision.date():
            raise EconomicSourceError("ECON_SOURCE_FUTURE_VINTAGE")
        rows = data.get("observations")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1_000:
        raise EconomicSourceError("ECON_SOURCE_ROWS_INVALID")
    parsed: list[ResearchObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EconomicSourceError("ECON_SOURCE_ROWS_INVALID")
        if provider == "BLS":
            period = row.get("period")
            if not isinstance(period, str) or period not in {f"M{x:02}" for x in range(1, 13)}:
                raise EconomicSourceError("ECON_SOURCE_MONTHLY_PERIOD_REQUIRED")
            stamp = _day(f"{row.get('year')}-{period[1:]}-01")
            start = end = None
            notes = row.get("footnotes")
            if not isinstance(notes, list) or any(not isinstance(n, dict) for n in notes):
                raise EconomicSourceError("ECON_SOURCE_FOOTNOTES_INVALID")
        else:
            stamp = _day(row.get("date"))
            period = stamp.isoformat()
            start, end = row.get("realtime_start"), row.get("realtime_end")
            if not _day(start) <= _day(query["realtime_start"][0]) <= _day(end):
                raise EconomicSourceError("ECON_SOURCE_VINTAGE_MISMATCH")
            if (
                not _day(query["observation_start"][0])
                <= stamp
                <= _day(query["observation_end"][0])
            ):
                raise EconomicSourceError("ECON_SOURCE_OBSERVATION_WINDOW_MISMATCH")
            notes = []
        if stamp > decision.date():
            raise EconomicSourceError("ECON_SOURCE_FUTURE_OBSERVATION")
        if frequency != "daily" and stamp.day != 1:
            raise EconomicSourceError("ECON_SOURCE_PERIOD_ALIGNMENT_INVALID")
        if frequency == "quarterly" and stamp.month not in {1, 4, 7, 10}:
            raise EconomicSourceError("ECON_SOURCE_PERIOD_ALIGNMENT_INVALID")
        raw_value = row.get("value")
        numeric_value = _number(raw_value)
        parsed.append(
            ResearchObservation(
                period,
                stamp,
                str(raw_value),
                numeric_value,
                start,
                end,
                json.dumps(notes, sort_keys=True),
            )
        )
    parsed.sort(key=lambda x: x.observation_date)
    if len({x.observation_date for x in parsed}) != len(parsed):
        raise EconomicSourceError("ECON_SOURCE_DUPLICATE_PERIOD")
    score = None
    direction, status = "NEUTRAL", "INSUFFICIENT_COMPARABLE_OBSERVATIONS"
    if len(parsed) >= 2:
        previous, latest = parsed[-2:]
        gap = (latest.observation_date - previous.observation_date).days
        months = (
            (latest.observation_date.year - previous.observation_date.year) * 12
            + latest.observation_date.month
            - previous.observation_date.month
        )
        adjacent = (
            gap == 1 if frequency == "daily" else months == (3 if frequency == "quarterly" else 1)
        )
        if adjacent and previous.numeric_value is not None and latest.numeric_value is not None:
            # Transient SQLAlchemy model only. No Session, flush, inserts, or time
            # assignment: a monthly period is not the event's publication time.
            event = EconomicEvent(
                event_key=f"research:{provider}:{response.series_id}",
                category=concept,
                actual_value=str(latest.numeric_value),
                previous_value=str(previous.numeric_value),
                forecast_value=None,
            )
            calculated = calculate_economic_features(event)
            score, direction = calculated["surprise_score"], calculated["direction"]
            status = "ACTUAL_VS_PREVIOUS_MOMENTUM_NOT_CONSENSUS_SURPRISE"
    return EconomicSourceFeatures(
        provider,
        response.series_id,
        concept,
        units,
        seasonal,
        frequency,
        response.original.sha256,
        response.original.url,
        receipt,
        decision,
        tuple(parsed),
        score,
        direction,
        status,
    )
