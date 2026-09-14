"""Pure public-page BRTI extraction for source evidence, never feed certification.

No HTTP, websocket, authentication or credential handling lives in this module.
Only the BRTI identity and original RTI points leave the supplied HTML parser.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from kalshi_predictor.overnight_paper.source_health import MAX_FORECAST_AGE_SECONDS, aware

PUBLIC_BRTI_URL = "https://www.cfbenchmarks.com/data/indices/BRTI"
MAX_PUBLIC_PAGE_BYTES = 8_000_000


class _NextData(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.matches = 0
        self.collecting = False
        self.closed = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        if any(key == "id" and value == "__NEXT_DATA__" for key, value in attrs):
            if len({key for key, _ in attrs}) != len(attrs):
                raise ValueError("CF_DUPLICATE_SCRIPT_ATTRIBUTE")
            self.matches += 1
            if self.matches != 1 or dict(attrs).get("type") != "application/json":
                raise ValueError("CF_SINGLE_JSON_SCRIPT_REQUIRED")
            self.collecting = True

    def handle_data(self, data: str) -> None:
        if self.collecting:
            self.chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.collecting:
            self.collecting = False
            self.closed = True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CF_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("CF_NONFINITE_JSON_NUMBER")


@dataclass(frozen=True)
class PublicBRTIEvidence:
    source_sha256: str
    received_at: datetime
    latest_observed_at: datetime
    window_start: datetime
    window_end: datetime
    points: tuple[tuple[str, str], ...]
    total_source_points: int
    provider_age_seconds: float
    window_age_seconds: float
    freshness_passed: bool
    freshness_blockers: tuple[str, ...]
    source_url: str = PUBLIC_BRTI_URL
    index: str = "BRTI"
    research_only: bool = True
    runtime_certified: bool = False


def extract_public_brti(
    *,
    html: bytes,
    expected_sha256: str,
    received_at: datetime,
    target_end: datetime,
    now: datetime,
    max_source_age_seconds: int = MAX_FORECAST_AGE_SECONDS,
) -> PublicBRTIEvidence:
    """Extract the complete standard 1Hz minute before ``target_end``.

    Original point clocks are preserved. An old window may be extracted as
    retrospective evidence, but freshness explicitly fails; it can never become
    runtime-certified merely by being downloaded again. The actual provider
    generation/update metadata needed by a live source adapter is not inferred
    from these observations. ``expected_sha256`` is the original response hash.
    """
    if (
        type(max_source_age_seconds) is not int
        or not 0 < max_source_age_seconds <= MAX_FORECAST_AGE_SECONDS
    ):
        raise ValueError("CF_FRESHNESS_LIMIT_REFUSED")
    if not isinstance(html, bytes) or not 0 < len(html) <= MAX_PUBLIC_PAGE_BYTES:
        raise ValueError("CF_PUBLIC_PAGE_SIZE_INVALID")
    if hashlib.sha256(html).hexdigest() != expected_sha256:
        raise ValueError("CF_ORIGINAL_RESPONSE_HASH_MISMATCH")
    receipt, boundary, reference = map(aware, (received_at, target_end, now))
    if reference < receipt or boundary > receipt or boundary.microsecond:
        raise ValueError("CF_RECEIPT_OR_TARGET_CLOCK_INVALID")
    parser = _NextData()
    try:
        parser.feed(html.decode("utf-8"))
        parser.close()
        if parser.matches != 1 or not parser.closed or parser.collecting:
            raise ValueError("CF_COMPLETE_NEXT_DATA_REQUIRED")
        root = json.loads(
            "".join(parser.chunks),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        # Explicit path only: do not recursively search unrelated page data.
        props = root["props"]["pageProps"]
        if props["externalIndexId"] != "BRTI":
            raise ValueError("CF_EXACT_BRTI_IDENTITY_REQUIRED")
        raw_points = props["indexConfig"]["rtis"]
        if not isinstance(raw_points, list) or not 60 <= len(raw_points) <= 100_000:
            raise ValueError("CF_RTI_POINTS_REQUIRED")
        points: list[tuple[datetime, str]] = []
        previous: int | None = None
        for point in raw_points:
            timestamp, raw_value = point["time"], point["value"]
            if type(timestamp) is not int or timestamp % 1000:
                raise ValueError("CF_TOP_OF_SECOND_TIMESTAMP_REQUIRED")
            if previous is not None and timestamp - previous != 1000:
                raise ValueError("CF_CONTIGUOUS_ORDERED_1HZ_REQUIRED")
            if not isinstance(raw_value, str):
                raise ValueError("CF_ORIGINAL_DECIMAL_STRING_REQUIRED")
            value = Decimal(raw_value)
            exponent = value.as_tuple().exponent
            if (
                not value.is_finite()
                or value <= 0
                or not isinstance(exponent, int)
                or exponent < -2
            ):
                raise ValueError("CF_POSITIVE_FINITE_CENT_PRECISION_REQUIRED")
            observed = datetime.fromtimestamp(timestamp // 1000, UTC)
            if observed > receipt:
                raise ValueError("CF_POINT_NOT_VISIBLE_AT_RECEIPT")
            points.append((observed, raw_value))
            previous = timestamp
        start = boundary - timedelta(seconds=60)
        selected = tuple((at.isoformat(), value) for at, value in points if start <= at < boundary)
        expected = tuple((start + timedelta(seconds=i)).isoformat() for i in range(60))
        if tuple(at for at, _ in selected) != expected:
            raise ValueError("CF_TARGET_MINUTE_INCOMPLETE")
    except (
        KeyError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        ArithmeticError,
        OverflowError,
        OSError,
    ):
        # Do not include arbitrary HTML/JSON values or parser excerpts in errors.
        raise ValueError("CF_PUBLIC_PAYLOAD_MALFORMED") from None
    latest = points[-1][0]
    provider_age = (reference - latest).total_seconds()
    window_age = (reference - (boundary - timedelta(seconds=1))).total_seconds()
    blockers = []
    if provider_age > max_source_age_seconds:
        blockers.append("CF_PROVIDER_OBSERVATIONS_STALE")
    if window_age > max_source_age_seconds:
        blockers.append("CF_TARGET_WINDOW_STALE")
    if (reference - receipt).total_seconds() > 60:
        blockers.append("CF_RECEIPT_STALE")
    return PublicBRTIEvidence(
        expected_sha256,
        receipt,
        latest,
        start,
        boundary,
        selected,
        len(points),
        provider_age,
        window_age,
        not blockers,
        tuple(blockers),
    )
