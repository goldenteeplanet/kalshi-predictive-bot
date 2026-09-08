"""Bounded FRED research originals; vintage dates are not release timestamps.

Official API: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
and https://fred.stlouisfed.org/docs/api/fred/series_release.html.
Current downloads, including revised values, do not prove historical availability.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

_BASE = "https://api.stlouisfed.org"
_SERIES = frozenset({"DFF", "CPIAUCSL", "UNRATE", "CPILFESL", "GDPC1", "PAYEMS"})
_PATHS = frozenset({"/fred/series/observations", "/fred/series/release"})
_MAX_BYTES = 1_048_576


class FREDError(RuntimeError):
    """Fixed diagnostic without request, response, or key."""


@dataclass(frozen=True)
class FREDOriginal:
    url: str
    received_at: datetime
    sha256: str
    payload: bytes = field(repr=False)
    research_only: bool = field(default=True, init=False)
    runtime_certified: bool = field(default=False, init=False)
    historical_availability_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class FREDResponse:
    kind: str
    series_id: str
    original: FREDOriginal
    data: dict[str, Any] = field(repr=False)
    provider_timestamp: None = None


def _day(value: Any) -> str:
    valid = False
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            valid = date.fromisoformat(value).isoformat() == value
        except ValueError:
            pass
    if not valid:
        raise FREDError("FRED_DATE_INVALID")
    return str(value)


def _window(start: Any, end: Any) -> None:
    if _day(start) > _day(end):
        raise FREDError("FRED_DATE_RANGE_INVALID")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError("nonfinite")


class _KeyTransport(httpx.BaseTransport):
    """Inject required query credential below HTTPX's request logging layer."""

    def __init__(self, key: str, inner: httpx.BaseTransport):
        self.__key = key
        self.__inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        safe_url = request.url
        if (
            request.method != "GET"
            or safe_url.scheme != "https"
            or safe_url.host != "api.stlouisfed.org"
            or safe_url.path not in _PATHS
            or "api_key" in safe_url.params
        ):
            raise FREDError("FRED_ENDPOINT_NOT_ALLOWED")
        request.url = safe_url.copy_add_param("api_key", self.__key)
        try:
            return self.__inner.handle_request(request)
        finally:
            request.url = safe_url

    def close(self) -> None:
        self.__key = ""
        self.__inner.close()


class FREDResearchClient:
    """Fixed-series GETs; local 1.05-second pacing, no retries or pagination."""

    def __init__(
        self,
        api_key: str,
        *,
        request_budget: int = 10,
        transport: httpx.MockTransport | None = None,
    ):
        if not isinstance(api_key, str) or re.fullmatch(r"[a-z0-9]{32}", api_key) is None:
            raise FREDError("FRED_KEY_FORMAT_INVALID")
        if type(request_budget) is not int or not 1 <= request_budget <= 25:
            raise FREDError("FRED_REQUEST_BUDGET_INVALID")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise FREDError("FRED_TEST_TRANSPORT_INVALID")
        self.__key = api_key
        self.__remaining = request_budget
        self.__halted = False
        self.__closed = False
        self.__last = float("-inf")
        self.__lock = threading.Lock()
        inner = (
            transport if transport is not None else httpx.HTTPTransport(retries=0, trust_env=False)
        )
        self.__client = httpx.Client(
            transport=_KeyTransport(api_key, inner),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10),
        )

    def __repr__(self) -> str:
        return f"FREDResearchClient(remaining_requests={self.__remaining})"

    def __enter__(self) -> FREDResearchClient:
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

    def _get(self, path: str, params: dict[str, str | int]) -> FREDResponse:
        allowed = {"series_id", "file_type", "realtime_start", "realtime_end"}
        if path == "/fred/series/observations":
            allowed |= {"observation_start", "observation_end", "limit", "sort_order"}
        if path not in _PATHS or not params.keys() <= allowed:
            raise FREDError("FRED_ENDPOINT_NOT_ALLOWED")
        if params.get("series_id") not in _SERIES or params.get("file_type") != "json":
            raise FREDError("FRED_SERIES_NOT_ALLOWED")
        with self.__lock:
            if self.__closed or self.__halted:
                raise FREDError("FRED_CLIENT_HALTED_OR_CLOSED")
            if any(self.__key in str(value) for value in params.values()):
                raise FREDError("FRED_SECRET_IN_QUERY")
            if self.__remaining <= 0:
                raise FREDError("FRED_LOCAL_QUOTA_EXHAUSTED")
            delay = 1.05 - (time.monotonic() - self.__last)
            if delay > 0:
                time.sleep(delay)
            self.__last = time.monotonic()
            self.__remaining -= 1
            try:
                with self.__client.stream(
                    "GET",
                    _BASE + path,
                    params=params,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        status = response.status_code
                        if status in {400, 401, 403, 429} or 300 <= status < 400:
                            self.__halted = True
                        code = {
                            400: "ARGUMENT_OR_KEY_REJECTED",
                            401: "AUTHENTICATION_FAILED",
                            403: "ACCESS_DENIED",
                            429: "RATE_OR_QUOTA_LIMITED",
                        }.get(status, "HTTP_FAILURE")
                        raise FREDError("FRED_" + code)
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise FREDError("FRED_ENCODING_REJECTED")
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise FREDError("FRED_CONTENT_TYPE_INVALID")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > _MAX_BYTES:
                            raise FREDError("FRED_RESPONSE_TOO_LARGE")
                        if time.monotonic() - self.__last > 60:
                            raise FREDError("FRED_RESPONSE_DEADLINE")
                        body.extend(chunk)
                    raw = bytes(body)
                    if self.__key.encode() in raw:
                        raise FREDError("FRED_CREDENTIAL_ECHO_REJECTED")
                    data = None
                    try:
                        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        pass
                    if not isinstance(data, dict):
                        raise FREDError("FRED_JSON_INVALID")
                    if self.__key in json.dumps(data):
                        raise FREDError("FRED_CREDENTIAL_ECHO_REJECTED")
                    return FREDResponse(
                        path.rsplit("/", 1)[1],
                        str(params["series_id"]),
                        FREDOriginal(
                            str(httpx.URL(_BASE + path, params=params)),
                            datetime.now(UTC),
                            hashlib.sha256(raw).hexdigest(),
                            raw,
                        ),
                        data,
                    )
            except httpx.HTTPError:
                pass
            raise FREDError("FRED_TRANSPORT_FAILED")

    def observations(
        self,
        *,
        series_id: str,
        observation_start: str,
        observation_end: str,
        realtime_start: str,
        realtime_end: str,
        limit: int = 100,
    ) -> FREDResponse:
        _window(observation_start, observation_end)
        _window(realtime_start, realtime_end)
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise FREDError("FRED_LIMIT_INVALID")
        result = self._get(
            "/fred/series/observations",
            {
                "series_id": series_id,
                "file_type": "json",
                "limit": limit,
                "sort_order": "asc",
                "observation_start": observation_start,
                "observation_end": observation_end,
                "realtime_start": realtime_start,
                "realtime_end": realtime_end,
            },
        )
        rows = result.data.get("observations")
        if not isinstance(rows, list) or len(rows) > limit:
            raise FREDError("FRED_SCHEMA_INVALID")
        previous = ""
        for row in rows:
            if not isinstance(row, dict):
                raise FREDError("FRED_SCHEMA_INVALID")
            day = _day(row.get("date"))
            if not observation_start <= day <= observation_end or day < previous:
                raise FREDError("FRED_OBSERVATION_RANGE_INVALID")
            previous = day
            _window(row.get("realtime_start"), row.get("realtime_end"))
            value = row.get("value")
            valid = value == "."
            if isinstance(value, str) and value != ".":
                try:
                    valid = Decimal(value).is_finite()
                except ArithmeticError:
                    pass
            if not valid:
                raise FREDError("FRED_VALUE_INVALID")
        return result

    def series_release(
        self,
        *,
        series_id: str,
        realtime_start: str,
        realtime_end: str,
    ) -> FREDResponse:
        _window(realtime_start, realtime_end)
        result = self._get(
            "/fred/series/release",
            {
                "series_id": series_id,
                "file_type": "json",
                "realtime_start": realtime_start,
                "realtime_end": realtime_end,
            },
        )
        rows = result.data.get("releases")
        if not isinstance(rows, list) or len(rows) > 100:
            raise FREDError("FRED_SCHEMA_INVALID")
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or not isinstance(row.get("name"), str)
            ):
                raise FREDError("FRED_SCHEMA_INVALID")
            _window(row.get("realtime_start"), row.get("realtime_end"))
        return result

    def observations_as_of(
        self,
        *,
        series_id: str,
        observation_start: str,
        observation_end: str,
        as_of: str,
        limit: int = 100,
    ) -> FREDResponse:
        """ALFRED snapshot via an explicit closed one-day real-time interval.

        https://fred.stlouisfed.org/docs/api/fred/realtime_period.html
        These are provider day-level vintages, not intraday publication evidence.
        The original receipt is today's receipt and never backdated to as_of.
        """
        _day(as_of)
        if as_of > datetime.now(UTC).date().isoformat():
            raise FREDError("FRED_FUTURE_VINTAGE_INVALID")
        return self.observations(
            series_id=series_id,
            observation_start=observation_start,
            observation_end=observation_end,
            realtime_start=as_of,
            realtime_end=as_of,
            limit=limit,
        )
