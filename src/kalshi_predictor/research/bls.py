"""Read-only BLS statistical POST retrieval; never certifies historical availability.

Official signature: https://www.bls.gov/developers/api_signature_v2.htm
Observation year/period and preliminary footnotes are preserved without an
invented publication timestamp, vintage, or historical decision-time claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
_SERIES = frozenset(
    {
        "CUUR0000SA0",
        "CUSR0000SA0",
        "CUSR0000SA0L1E",
        "LNS14000000",
        "CES0000000001",
        "CES0500000003",
        "WPSFD4",
    }
)
_MAX_BYTES = 1_048_576

# Identity metadata checked against official BLS pages on 2026-09-08.
# These are series labels, not individual dated release identities.
_TITLES = {
    "CUUR0000SA0": "CPI-U all items, U.S. city average, not seasonally adjusted",
    "CUSR0000SA0": "CPI-U all items, U.S. city average, seasonally adjusted",
    "CUSR0000SA0L1E": "CPI-U all items less food and energy, seasonally adjusted",
    "LNS14000000": "Unemployment rate, seasonally adjusted",
    "CES0000000001": "All employees, total nonfarm, thousands, seasonally adjusted",
    "CES0500000003": "Average hourly earnings, all employees, total private, seasonally adjusted",
    "WPSFD4": "PPI final demand, seasonally adjusted",
}


@dataclass(frozen=True)
class BLSSeriesMetadata:
    series_id: str
    title: str
    official_source: str
    release_identity: None = None
    provider_timestamp: None = None


def series_metadata(series_id: str) -> BLSSeriesMetadata:
    """Verified labels, independent of whether a current API capture succeeded."""
    if series_id not in _SERIES:
        raise BLSError("BLS_SERIES_NOT_ALLOWED")
    source = "https://data.bls.gov/timeseries/" + series_id
    if series_id == "CUSR0000SA0L1E":
        source = "https://download.bls.gov/pub/time.series/cu/cu.series"
    elif series_id == "WPSFD4":
        source = "https://www.bls.gov/web/ppi/ppi-fdidsf.htm"
    return BLSSeriesMetadata(series_id, _TITLES[series_id], source)


class BLSError(RuntimeError):
    """Fixed code without original HTTP exceptions or response messages."""


@dataclass(frozen=True)
class BLSOriginal:
    url: str
    received_at: datetime
    sha256: str
    payload: bytes = field(repr=False)
    research_only: bool = field(default=True, init=False)
    runtime_certified: bool = field(default=False, init=False)
    historical_availability_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class BLSResponse:
    kind: str
    series_id: str
    original: BLSOriginal
    data: dict[str, Any] = field(repr=False)
    provider_timestamp: None = None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError("nonfinite")


def _validate(data: dict[str, Any], series_id: str, year: int, catalog: bool = False) -> None:
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise BLSError("BLS_SEMANTIC_REQUEST_FAILED")
    # The official signature examples show a one-element Results list; current
    # JSON responses also use an object. Preserve either original representation.
    results = data.get("Results")
    if isinstance(results, list) and len(results) == 1:
        results = results[0]
    if not isinstance(results, dict):
        raise BLSError("BLS_SCHEMA_INVALID")
    series = results.get("series")
    if not isinstance(series, list) or len(series) != 1:
        raise BLSError("BLS_SERIES_IDENTITY_MISMATCH")
    item = series[0]
    if not isinstance(item, dict) or item.get("seriesID") != series_id:
        raise BLSError("BLS_SERIES_IDENTITY_MISMATCH")
    if catalog:
        metadata = item.get("catalog")
        if (
            not isinstance(metadata, dict)
            or metadata.get("series_id") != series_id
            or not isinstance(metadata.get("series_title"), str)
            or not metadata["series_title"].strip()
        ):
            raise BLSError("BLS_CATALOG_MISSING_OR_IDENTITY_MISMATCH")
    rows = item.get("data")
    if not isinstance(rows, list) or not rows or len(rows) > 13:
        raise BLSError("BLS_EMPTY_OR_INVALID_DATA")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("year") != str(year):
            raise BLSError("BLS_YEAR_MISMATCH")
        period = row.get("period")
        if not isinstance(period, str) or re.fullmatch(r"M(0[1-9]|1[0-3])", period) is None:
            raise BLSError("BLS_PERIOD_INVALID")
        if period in seen:
            raise BLSError("BLS_DUPLICATE_PERIOD")
        seen.add(period)
        # Retain missing/suppression symbols exactly; never coerce them to zero.
        value = row.get("value")
        if not isinstance(value, str) or not 1 <= len(value) <= 100:
            raise BLSError("BLS_VALUE_INVALID")
        footnotes = row.get("footnotes")
        if not isinstance(footnotes, list) or len(footnotes) > 50:
            raise BLSError("BLS_FOOTNOTES_INVALID")
        for note in footnotes:
            if not isinstance(note, dict) or any(not isinstance(v, str) for v in note.values()):
                raise BLSError("BLS_FOOTNOTES_INVALID")


class BLSResearchClient:
    """One allowed series and one year per request; local bounded research only."""

    def __init__(
        self,
        api_key: str,
        *,
        request_budget: int = 10,
        transport: httpx.MockTransport | None = None,
    ):
        if not isinstance(api_key, str) or re.fullmatch(r"[A-Za-z0-9]{32}", api_key) is None:
            raise BLSError("BLS_KEY_FORMAT_INVALID")
        if type(request_budget) is not int or not 1 <= request_budget <= 25:
            raise BLSError("BLS_REQUEST_BUDGET_INVALID")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise BLSError("BLS_TEST_TRANSPORT_INVALID")
        self.__key = api_key
        self.__remaining = request_budget
        self.__closed = False
        self.__halted = False
        self.__last = float("-inf")
        self.__lock = threading.Lock()
        self.__client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def __repr__(self) -> str:
        return f"BLSResearchClient(remaining_requests={self.__remaining})"

    def __enter__(self) -> BLSResearchClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self.__lock:
            self.__closed = True
            self.__key = ""
            self.__client.close()

    @property
    def remaining_requests(self) -> int:
        return self.__remaining

    def observations(self, *, series_id: str, year: int, catalog: bool = False) -> BLSResponse:
        if not isinstance(series_id, str) or series_id not in _SERIES:
            raise BLSError("BLS_SERIES_NOT_ALLOWED")
        if type(year) is not int or not 1900 <= year <= datetime.now(UTC).year:
            raise BLSError("BLS_YEAR_INVALID")
        if type(catalog) is not bool:
            raise BLSError("BLS_CATALOG_ARGUMENT_INVALID")
        with self.__lock:
            if self.__closed or self.__halted:
                raise BLSError("BLS_CLIENT_HALTED_OR_CLOSED")
            if self.__remaining <= 0:
                raise BLSError("BLS_LOCAL_QUOTA_EXHAUSTED")
            delay = 1.05 - (time.monotonic() - self.__last)
            if delay > 0:
                time.sleep(delay)
            self.__last = time.monotonic()
            self.__remaining -= 1
            try:
                request_data: dict[str, Any] = {
                    "seriesid": [series_id],
                    "startyear": str(year),
                    "endyear": str(year),
                    "registrationkey": self.__key,
                }
                if catalog:
                    request_data["catalog"] = True
                with self.__client.stream(
                    "POST",
                    _URL,
                    json=request_data,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    status = response.status_code
                    if status != 200:
                        if status in {400, 401, 403, 429} or 300 <= status < 400:
                            self.__halted = True
                        code = {
                            400: "ARGUMENT_OR_KEY_REJECTED",
                            401: "AUTHENTICATION_FAILED",
                            403: "ACCESS_DENIED",
                            429: "RATE_OR_QUOTA_LIMITED",
                        }.get(status, "HTTP_FAILURE")
                        raise BLSError("BLS_" + code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise BLSError("BLS_ENCODING_REJECTED")
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type.strip().lower() != "application/json":
                        raise BLSError("BLS_CONTENT_TYPE_INVALID")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > _MAX_BYTES:
                            raise BLSError("BLS_RESPONSE_TOO_LARGE")
                        if time.monotonic() - self.__last > 60:
                            raise BLSError("BLS_RESPONSE_DEADLINE")
                        body.extend(chunk)
                    raw = bytes(body)
                    if self.__key.encode() in raw:
                        raise BLSError("BLS_CREDENTIAL_ECHO_REJECTED")
                    data = None
                    try:
                        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        pass
                    if not isinstance(data, dict):
                        raise BLSError("BLS_JSON_INVALID")
                    if self.__key in json.dumps(data):
                        raise BLSError("BLS_CREDENTIAL_ECHO_REJECTED")
                    if data.get("status") != "REQUEST_SUCCEEDED":
                        self.__halted = True
                    _validate(data, series_id, year, catalog)
                    return BLSResponse(
                        "observations",
                        series_id,
                        BLSOriginal(_URL, datetime.now(UTC), hashlib.sha256(raw).hexdigest(), raw),
                        data,
                    )
            except httpx.HTTPError:
                pass
            # Outside except: no retained request/body-bearing exception context.
            raise BLSError("BLS_TRANSPORT_FAILED")
