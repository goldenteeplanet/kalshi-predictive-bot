"""Bounded Unusual Whales market research; never authorizes a paper trade.

Official API/docs checked 2026-09-08. Prediction asset IDs are vendor outcome IDs,
not verified Kalshi tickers. Provider data is personal/internal research only.
No user positions, account, order, alert mutation, or arbitrary URL is supported.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

_BASE = "https://api.unusualwhales.com"
_UNUSUAL = "/api/predictions/unusual"
_CALENDAR = "/api/market/economic-calendar"
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_DETAIL = re.compile(r"/api/predictions/market/([A-Za-z0-9][A-Za-z0-9_.-]{0,199})(/liquidity)?")
_MAX_BYTES = 1_048_576
_PACING_SECONDS = 1.05


class UnusualWhalesError(RuntimeError):
    """A fixed diagnostic code; no HTTP request, response body, or credential."""

    def __init__(self, code: str, *, retry_after_seconds: int | None = None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class UnusualWhalesOriginal:
    url: str
    received_at: datetime
    sha256: str
    payload: bytes = field(repr=False)
    research_only: bool = field(default=True, init=False)
    runtime_certified: bool = field(default=False, init=False)
    venue_mapping_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class UnusualWhalesResponse:
    kind: str
    original: UnusualWhalesOriginal
    data: dict[str, Any] | list[dict[str, Any]] = field(repr=False)
    # This is vendor observation time only when explicitly supplied. Calendar
    # event time and market resolves/end_date are NOT publication timestamps.
    provider_timestamp: datetime | None = None


def _invalid_constant(_: str) -> None:
    raise ValueError("nonfinite JSON")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _asset_id(value: object) -> str:
    if not isinstance(value, str) or _ASSET.fullmatch(value) is None:
        raise UnusualWhalesError("UW_ASSET_ID_INVALID")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise UnusualWhalesError("UW_SCHEMA_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise UnusualWhalesError("UW_SCHEMA_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UnusualWhalesError("UW_SCHEMA_INVALID")
    return parsed.astimezone(UTC)


class UnusualWhalesResearchClient:
    """Four named GET methods with a per-instance attempt budget.

    The local default is ten attempts, at least 1.05 seconds apart, 1 MiB per
    response and a 60-second response deadline. These are conservative local
    bounds, not assertions about the account's entitlement or daily quota.
    Only an exact MockTransport may be injected for isolated offline tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        request_budget: int = 10,
        transport: httpx.MockTransport | None = None,
    ):
        if (
            not isinstance(api_key, str)
            or not 8 <= len(api_key) <= 4096
            or re.fullmatch(r"[A-Za-z0-9._~+/=-]+", api_key) is None
        ):
            raise UnusualWhalesError("UW_KEY_FORMAT_INVALID")
        if type(request_budget) is not int or not 1 <= request_budget <= 25:
            raise UnusualWhalesError("UW_REQUEST_BUDGET_INVALID")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise UnusualWhalesError("UW_TEST_TRANSPORT_INVALID")
        self.__key = api_key
        self.__remaining = request_budget
        self.__halted = False
        self.__closed = False
        self.__last_attempt = float("-inf")
        self.__lock = threading.Lock()
        self.__client = httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def __repr__(self) -> str:
        return f"UnusualWhalesResearchClient(remaining_requests={self.__remaining})"

    def __enter__(self) -> UnusualWhalesResearchClient:
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

    def _get(self, path: str, params: dict[str, str | int]) -> tuple[Any, UnusualWhalesOriginal]:
        if path not in {_UNUSUAL, _CALENDAR} and _DETAIL.fullmatch(path) is None:
            raise UnusualWhalesError("UW_ENDPOINT_NOT_ALLOWED")
        allowed = {"categories", "limit", "offset"} if path == _UNUSUAL else set()
        if not params.keys() <= allowed:
            raise UnusualWhalesError("UW_QUERY_NOT_ALLOWED")
        with self.__lock:
            if self.__closed:
                raise UnusualWhalesError("UW_CLIENT_CLOSED")
            if self.__key in path or any(self.__key in str(value) for value in params.values()):
                raise UnusualWhalesError("UW_SECRET_IN_URL_REJECTED")
            if self.__halted:
                raise UnusualWhalesError("UW_CLIENT_HALTED")
            if self.__remaining <= 0:
                raise UnusualWhalesError("UW_LOCAL_QUOTA_EXHAUSTED")
            delay = _PACING_SECONDS - (time.monotonic() - self.__last_attempt)
            if delay > 0:
                time.sleep(delay)
            self.__last_attempt = time.monotonic()
            self.__remaining -= 1
            try:
                with self.__client.stream(
                    "GET",
                    _BASE + path,
                    params=params,
                    headers={
                        "Authorization": "Bearer " + self.__key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    status = response.status_code
                    if status != 200:
                        if status in {401, 403, 429} or 300 <= status < 400:
                            self.__halted = True
                        codes = {
                            401: "AUTHENTICATION_FAILED",
                            403: "ACCESS_OR_TIER_DENIED",
                            404: "NOT_FOUND",
                            422: "ARGUMENT_REJECTED",
                            429: "RATE_OR_QUOTA_LIMITED",
                        }
                        retry = response.headers.get("retry-after", "")
                        seconds = (
                            min(int(retry), 86_400) if re.fullmatch(r"[0-9]{1,7}", retry) else None
                        )
                        code = (
                            "REDIRECT_REJECTED"
                            if 300 <= status < 400
                            else codes.get(status, "HTTP_FAILURE")
                        )
                        raise UnusualWhalesError("UW_" + code, retry_after_seconds=seconds)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise UnusualWhalesError("UW_CONTENT_ENCODING_REJECTED")
                    if (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                        != "application/json"
                    ):
                        raise UnusualWhalesError("UW_CONTENT_TYPE_INVALID")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > _MAX_BYTES:
                            raise UnusualWhalesError("UW_RESPONSE_TOO_LARGE")
                        if time.monotonic() - self.__last_attempt > 60:
                            raise UnusualWhalesError("UW_RESPONSE_DEADLINE")
                        body.extend(chunk)
                    raw = bytes(body)
                    if self.__key.encode() in raw:
                        raise UnusualWhalesError("UW_CREDENTIAL_ECHO_REJECTED")
                    try:
                        data = json.loads(
                            raw,
                            parse_float=Decimal,
                            parse_constant=_invalid_constant,
                            object_pairs_hook=_unique_object,
                        )
                    except (ValueError, UnicodeError, RecursionError):
                        raise UnusualWhalesError("UW_JSON_INVALID") from None
                    if self.__key in json.dumps(data, default=str):
                        raise UnusualWhalesError("UW_CREDENTIAL_ECHO_REJECTED")
                    if not isinstance(data, dict) or "data" not in data:
                        raise UnusualWhalesError("UW_SCHEMA_INVALID")
                    original = UnusualWhalesOriginal(
                        str(httpx.URL(_BASE + path, params=params)),
                        datetime.now(UTC),
                        hashlib.sha256(raw).hexdigest(),
                        raw,
                    )
                    return data["data"], original
            except httpx.HTTPError:
                # Never retain httpx's request (which carries Authorization).
                pass
            raise UnusualWhalesError("UW_TRANSPORT_FAILED")

    def unusual_markets(
        self, *, categories: str | None = None, limit: int = 10, offset: int = 0
    ) -> UnusualWhalesResponse:
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or type(offset) is not int
            or not 0 <= offset <= 5_000
        ):
            raise UnusualWhalesError("UW_PAGE_BOUND_INVALID")
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if categories is not None:
            if (
                not isinstance(categories, str)
                or re.fullmatch(r"[A-Za-z, ]{1,100}", categories) is None
            ):
                raise UnusualWhalesError("UW_CATEGORIES_INVALID")
            params["categories"] = categories
        data, original = self._get(_UNUSUAL, params)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("data"), list)
            or len(data["data"]) > limit
        ):
            raise UnusualWhalesError("UW_SCHEMA_INVALID")
        for row in data["data"]:
            if not isinstance(row, dict):
                raise UnusualWhalesError("UW_SCHEMA_INVALID")
            _asset_id(row.get("asset_id"))
        return UnusualWhalesResponse("unusual_markets", original, data)

    def market_details(self, *, asset_id: str) -> UnusualWhalesResponse:
        data, original = self._get("/api/predictions/market/" + _asset_id(asset_id), {})
        if not isinstance(data, dict) or data.get("asset_id") != asset_id:
            raise UnusualWhalesError("UW_ASSET_IDENTITY_MISMATCH")
        return UnusualWhalesResponse("market_details", original, data)

    def market_liquidity(self, *, asset_id: str) -> UnusualWhalesResponse:
        data, original = self._get(
            "/api/predictions/market/" + _asset_id(asset_id) + "/liquidity", {}
        )
        if not isinstance(data, dict) or data.get("asset_id") != asset_id:
            raise UnusualWhalesError("UW_ASSET_IDENTITY_MISMATCH")
        for side in ("bids", "asks"):
            if not isinstance(data.get(side), list) or len(data[side]) > 10_000:
                raise UnusualWhalesError("UW_SCHEMA_INVALID")
            for level in data[side]:
                if not isinstance(level, dict) or not {"price", "size"} <= level.keys():
                    raise UnusualWhalesError("UW_SCHEMA_INVALID")
                if any(isinstance(level[key], bool) for key in ("price", "size")):
                    raise UnusualWhalesError("UW_SCHEMA_INVALID")
                try:
                    price, size = Decimal(level["price"]), Decimal(level["size"])
                except (ValueError, TypeError, ArithmeticError):
                    raise UnusualWhalesError("UW_SCHEMA_INVALID") from None
                if not price.is_finite() or not size.is_finite() or not 0 <= price <= 1 or size < 0:
                    raise UnusualWhalesError("UW_SCHEMA_INVALID")
        stamp = None if data.get("timestamp") is None else _timestamp(data["timestamp"])
        return UnusualWhalesResponse("market_liquidity", original, data, stamp)

    def economic_calendar(self) -> UnusualWhalesResponse:
        data, original = self._get(_CALENDAR, {})
        if not isinstance(data, list) or len(data) > 5_000:
            raise UnusualWhalesError("UW_SCHEMA_INVALID")
        for row in data:
            if not isinstance(row, dict) or not isinstance(row.get("event"), str):
                raise UnusualWhalesError("UW_SCHEMA_INVALID")
            _timestamp(row.get("time"))
        return UnusualWhalesResponse("economic_calendar", original, data)
