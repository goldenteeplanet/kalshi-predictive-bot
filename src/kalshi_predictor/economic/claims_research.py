"""Pure ICSA initial-release input panel; no forecasts, database or gate verdicts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from kalshi_predictor.research.fred import FREDResponse


class ClaimsSourceError(ValueError):
    """Fixed diagnostic, never includes source bodies or request credentials."""


@dataclass(frozen=True)
class InitialClaimsObservation:
    week_ending: date
    raw_value: str
    persons: Decimal | None
    realtime_start: date
    realtime_end: date
    publication_at: None = None


@dataclass(frozen=True)
class InitialClaimsPanel:
    observations: tuple[InitialClaimsObservation, ...]
    missing_weeks: tuple[date, ...]
    observation_start: date
    observation_end: date
    as_of: date
    received_at: datetime
    decision_at: datetime
    source_url: str
    source_sha256: str
    series_id: str = field(default="ICSA", init=False)
    units: str = field(default="persons", init=False)
    seasonal_adjustment: str = field(default="SA", init=False)
    frequency: str = field(default="weekly_ending_saturday", init=False)
    release_selection: str = field(default="FRED_OUTPUT_TYPE_4_INITIAL_RELEASE", init=False)
    historical_availability_verified: bool = field(default=False, init=False)
    dol_first_release_verified: bool = field(default=False, init=False)
    runtime_certified: bool = field(default=False, init=False)
    research_only: bool = field(default=True, init=False)


def _day(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ClaimsSourceError("CLAIMS_DATE_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ClaimsSourceError("CLAIMS_DATE_INVALID") from None


def _clock(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ClaimsSourceError("CLAIMS_CLOCK_INVALID")
    return value.astimezone(UTC)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ClaimsSourceError("CLAIMS_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ClaimsSourceError("CLAIMS_NONFINITE_JSON")


def build_initial_claims_panel(
    response: FREDResponse, *, decision_at: datetime
) -> InitialClaimsPanel:
    """Validate full output4 coverage without asserting historical availability.

    ICSA's fixed semantic mapping is SA weekly persons, identity units. Metadata
    and original DOL release cross-checks remain necessary for runtime adoption.
    A real-time date is never promoted into an exact publication timestamp.
    """
    if type(response) is not FREDResponse or response.kind != "observations":
        raise ClaimsSourceError("CLAIMS_RESPONSE_INVALID")
    if response.series_id != "ICSA":
        raise ClaimsSourceError("CLAIMS_SERIES_INVALID")
    receipt, decision = _clock(response.original.received_at), _clock(decision_at)
    if receipt > decision:
        raise ClaimsSourceError("CLAIMS_NOT_RECEIVED_AT_DECISION")
    raw = response.original.payload
    if not raw or len(raw) > 1_048_576:
        raise ClaimsSourceError("CLAIMS_BODY_INVALID")
    if hashlib.sha256(raw).hexdigest() != response.original.sha256:
        raise ClaimsSourceError("CLAIMS_HASH_MISMATCH")
    try:
        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ClaimsSourceError("CLAIMS_JSON_INVALID") from None
    if not isinstance(data, dict) or data != response.data:
        raise ClaimsSourceError("CLAIMS_BODY_MISMATCH")
    if data.get("series_id", "ICSA") != "ICSA":
        raise ClaimsSourceError("CLAIMS_BODY_SERIES_MISMATCH")
    url = urlsplit(response.original.url)
    query = parse_qs(url.query, keep_blank_values=True)
    required = {
        "series_id",
        "file_type",
        "observation_start",
        "observation_end",
        "realtime_start",
        "realtime_end",
        "limit",
        "sort_order",
        "output_type",
        "units",
        "offset",
    }
    if (
        url.scheme != "https"
        or url.netloc != "api.stlouisfed.org"
        or url.path != "/fred/series/observations"
        or url.fragment
        or query.keys() != required
        or any(len(v) != 1 for v in query.values())
    ):
        raise ClaimsSourceError("CLAIMS_QUERY_INVALID")
    fixed = {
        "series_id": "ICSA",
        "file_type": "json",
        "sort_order": "asc",
        "output_type": "4",
        "units": "lin",
        "offset": "0",
    }
    if any(query[k] != [v] for k, v in fixed.items()):
        raise ClaimsSourceError("CLAIMS_QUERY_MODE_INVALID")
    if not re.fullmatch(r"[1-9]\d{0,3}", query["limit"][0]):
        raise ClaimsSourceError("CLAIMS_LIMIT_INVALID")
    limit = int(query["limit"][0])
    if limit > 1_000:
        raise ClaimsSourceError("CLAIMS_LIMIT_INVALID")
    start, end, as_of = (
        _day(query[k][0]) for k in ("observation_start", "observation_end", "realtime_end")
    )
    if not start <= end <= as_of <= min(receipt.date(), decision.date()):
        raise ClaimsSourceError("CLAIMS_WINDOW_INVALID")
    if (end - start).days > 6999:
        raise ClaimsSourceError("CLAIMS_WINDOW_TOO_LARGE")
    if _day(query["realtime_start"][0]) != start:
        raise ClaimsSourceError("CLAIMS_REALTIME_WINDOW_INVALID")
    if data.get("units") != "lin" or type(data.get("output_type")) is not int:
        raise ClaimsSourceError("CLAIMS_TRANSFORM_INVALID")
    if data["output_type"] != 4:
        raise ClaimsSourceError("CLAIMS_INITIAL_RELEASE_REQUIRED")
    for key in ("observation_start", "observation_end", "realtime_start", "realtime_end"):
        if data.get(key) != query[key][0]:
            raise ClaimsSourceError("CLAIMS_RESPONSE_WINDOW_MISMATCH")
    rows = data.get("observations")
    if not isinstance(rows, list):
        raise ClaimsSourceError("CLAIMS_ROWS_INVALID")
    if (
        type(data.get("count")) is not int
        or data["count"] != len(rows)
        or len(rows) > limit
        or type(data.get("offset")) is not int
        or data["offset"] != 0
        or type(data.get("limit")) is not int
        or data["limit"] != limit
    ):
        raise ClaimsSourceError("CLAIMS_INCOMPLETE_RESPONSE")
    parsed: list[InitialClaimsObservation] = []
    previous: date | None = None
    for row in rows:
        if not isinstance(row, dict):
            raise ClaimsSourceError("CLAIMS_ROW_INVALID")
        week = _day(row.get("date"))
        release_start, release_end = _day(row.get("realtime_start")), _day(row.get("realtime_end"))
        if week.weekday() != 5 or not start <= week <= end:
            raise ClaimsSourceError("CLAIMS_SATURDAY_WINDOW_REQUIRED")
        if previous is not None and week <= previous:
            raise ClaimsSourceError("CLAIMS_WEEK_ORDER_OR_DUPLICATE")
        if not week <= release_start <= as_of or release_end < release_start:
            raise ClaimsSourceError("CLAIMS_RELEASE_WINDOW_INVALID")
        value = row.get("value")
        if not isinstance(value, str) or not 1 <= len(value) <= 100:
            raise ClaimsSourceError("CLAIMS_VALUE_INVALID")
        number = None
        if value != ".":
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise ClaimsSourceError("CLAIMS_VALUE_INVALID") from None
            if not number.is_finite() or not 0 <= number <= 1_000_000_000:
                raise ClaimsSourceError("CLAIMS_VALUE_INVALID")
            if number != number.to_integral_value():
                raise ClaimsSourceError("CLAIMS_INTEGRAL_PERSONS_REQUIRED")
        parsed.append(InitialClaimsObservation(week, value, number, release_start, release_end))
        previous = week
    present = {row.week_ending for row in parsed if row.persons is not None}
    missing = []
    week = start + timedelta(days=(5 - start.weekday()) % 7)
    while week <= end:
        if week not in present:
            missing.append(week)
        week += timedelta(days=7)
    return InitialClaimsPanel(
        tuple(parsed),
        tuple(missing),
        start,
        end,
        as_of,
        receipt,
        decision,
        response.original.url,
        response.original.sha256,
    )
