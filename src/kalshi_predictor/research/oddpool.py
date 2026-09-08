"""Bounded read-only Oddpool research; never an admission or settlement source.

Official docs checked 2026-09-08:
https://docs.oddpool.com/search/search-markets
https://docs.oddpool.com/kalshi/orderbook
https://docs.oddpool.com/authentication
https://docs.oddpool.com/rate-limits

Search/historical: Free; whale tracking: Pro; arbitrage/spreads: Premium.
Institutional /reference/v2 is a separate entitlement and is not implemented.
Free limits: 1 request/second, 1,000/month. The local request budget is not an
account-wide quota ledger. No automatic retries or tier upgrades are performed.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

_BASE = "https://api.oddpool.com"
_PATHS = frozenset({"/search/markets", "/historical/kalshi/orderbook"})
_MAX_BYTES = 1_048_576


class OddpoolError(RuntimeError):
    """Fixed public error code only; never retains a response or request object."""

    def __init__(self, code: str, *, retry_after_seconds: int | None = None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class OriginalResponse:
    url: str
    received_at: datetime
    sha256: str
    payload: bytes = field(repr=False)
    provenance_role: str = "THIRD_PARTY_HISTORICAL_RESEARCH_ONLY"


@dataclass(frozen=True)
class Market:
    market_id: str
    exchange: str
    question: str
    status: str
    last_yes_price: Decimal | None
    last_no_price: Decimal | None


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class BookSnapshot:
    market_id: str
    timestamp_ms: int
    yes_bids: tuple[BookLevel, ...]
    no_bids: tuple[BookLevel, ...]
    best_yes_bid: Decimal | None
    best_yes_ask: Decimal | None


@dataclass(frozen=True)
class OddpoolPage:
    records: tuple[Market | BookSnapshot, ...]
    original: OriginalResponse
    has_more: bool
    next_cursor: str | None = None
    next_offset: int | None = None


def _number(value: object, *, probability: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise OddpoolError("ODDPOOL_SCHEMA_INVALID") from None
    if not result.is_finite() or result < 0 or (probability and result > 1):
        raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
    return result


def _optional_price(value: object) -> Decimal | None:
    return None if value is None else _number(value, probability=True)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 10_000:
        raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
    return value


def _levels(value: object) -> tuple[BookLevel, ...]:
    if not isinstance(value, list) or len(value) > 10_000:
        raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
    result = []
    for level in value:
        if not isinstance(level, dict):
            raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
        result.append(
            BookLevel(_number(level.get("price"), probability=True), _number(level.get("size")))
        )
    prices = [level.price for level in result]
    if prices != sorted(set(prices), reverse=True):
        raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
    return tuple(result)


class OddpoolResearchClient:
    """Server-side secret injection; only the two named GET methods are public.

    One page is one charged attempt, including errors. Default budget ten requests
    per client; pacing is at least 1.05 seconds between attempts. Callers must
    coordinate account-wide quota separately. MockTransport is supported for
    offline tests; arbitrary preconfigured clients and base URLs are not accepted.
    """

    def __init__(
        self,
        api_key: str,
        *,
        request_budget: int = 10,
        transport: httpx.MockTransport | None = None,
    ):
        if not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", api_key):
            raise OddpoolError("ODDPOOL_KEY_FORMAT_INVALID")
        if type(request_budget) is not int or not 1 <= request_budget <= 100:
            raise OddpoolError("ODDPOOL_REQUEST_BUDGET_INVALID")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise OddpoolError("ODDPOOL_TEST_TRANSPORT_INVALID")
        self.__key = api_key
        self.__remaining = request_budget
        self.__last_attempt = float("-inf")
        self.__lock = threading.Lock()
        self.__client = httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(10.0),
            transport=transport,
            limits=httpx.Limits(max_connections=1),
        )

    def __enter__(self) -> OddpoolResearchClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.__client.close()

    @property
    def remaining_requests(self) -> int:
        return self.__remaining

    def _get(self, path: str, params: dict[str, str | int]) -> tuple[Any, OriginalResponse]:
        if path not in _PATHS:
            raise OddpoolError("ODDPOOL_ENDPOINT_NOT_ALLOWED")
        if any(self.__key in str(value) for value in params.values()):
            raise OddpoolError("ODDPOOL_SECRET_IN_QUERY_REJECTED")
        with self.__lock:
            if self.__remaining <= 0:
                raise OddpoolError("ODDPOOL_LOCAL_QUOTA_EXHAUSTED")
            delay = 1.05 - (time.monotonic() - self.__last_attempt)
            if delay > 0:
                time.sleep(delay)
            self.__remaining -= 1
            self.__last_attempt = time.monotonic()
            try:
                with self.__client.stream(
                    "GET",
                    _BASE + path,
                    params=params,
                    headers={
                        "X-API-Key": self.__key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    status = response.status_code
                    if status != 200:
                        codes = {
                            401: "AUTHENTICATION_FAILED",
                            403: "TIER_OR_ACCESS_DENIED",
                            429: "RATE_OR_QUOTA_LIMITED",
                        }
                        retry = response.headers.get("retry-after", "")
                        seconds = (
                            min(int(retry), 86400)
                            if retry.isascii() and retry.isdigit() and len(retry) < 8
                            else None
                        )
                        code = (
                            "REDIRECT_REJECTED"
                            if 300 <= status < 400
                            else codes.get(status, "HTTP_FAILURE")
                        )
                        raise OddpoolError("ODDPOOL_" + code, retry_after_seconds=seconds)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise OddpoolError("ODDPOOL_CONTENT_ENCODING_REJECTED")
                    if (
                        response.headers.get("content-type", "").split(";")[0].strip()
                        != "application/json"
                    ):
                        raise OddpoolError("ODDPOOL_CONTENT_TYPE_INVALID")
                    chunks = bytearray()
                    for chunk in response.iter_bytes(chunk_size=16_384):
                        if len(chunks) + len(chunk) > _MAX_BYTES:
                            raise OddpoolError("ODDPOOL_RESPONSE_TOO_LARGE")
                        if time.monotonic() - self.__last_attempt > 30:
                            raise OddpoolError("ODDPOOL_RESPONSE_DEADLINE")
                        chunks.extend(chunk)
                    raw = bytes(chunks)
                    if self.__key.encode() in raw:
                        raise OddpoolError("ODDPOOL_CREDENTIAL_ECHO_REJECTED")
                    invalid_json = False
                    try:
                        parsed = json.loads(raw, parse_float=Decimal)
                    except (ValueError, UnicodeError):
                        invalid_json = True
                    if invalid_json:
                        raise OddpoolError("ODDPOOL_JSON_INVALID")
                    if self.__key in json.dumps(parsed, default=str):
                        raise OddpoolError("ODDPOOL_CREDENTIAL_ECHO_REJECTED")
                    original = OriginalResponse(
                        str(response.url), datetime.now(UTC), hashlib.sha256(raw).hexdigest(), raw
                    )
                    return parsed, original
            except httpx.HTTPError:
                pass
        # Leave the exception handler before raising: from None only hides a
        # retained HTTPError context, which can still contain credential headers.
        raise OddpoolError("ODDPOOL_TRANSPORT_FAILED")

    def search_markets(
        self,
        *,
        q: str | None = None,
        series_id: str | None = None,
        exchange: str = "kalshi",
        status: str = "active",
        limit: int = 20,
        offset: int = 0,
    ) -> OddpoolPage:
        if (
            not (q or series_id)
            or exchange not in {"kalshi", "polymarket"}
            or status not in {"active", "closed"}
        ):
            raise OddpoolError("ODDPOOL_SEARCH_ARGUMENT_INVALID")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10_000
        ):
            raise OddpoolError("ODDPOOL_PAGE_BOUND_INVALID")
        params: dict[str, str | int] = dict(
            exchange=exchange, status=status, limit=limit, offset=offset
        )
        for name, value in (("q", q), ("series_id", series_id)):
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 256:
                    raise OddpoolError("ODDPOOL_SEARCH_ARGUMENT_INVALID")
                params[name] = value
        data, original = self._get("/search/markets", params)
        if not isinstance(data, list) or len(data) > limit:
            raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
        rows = []
        for row in data:
            if (
                not isinstance(row, dict)
                or row.get("exchange") != exchange
                or row.get("status") != status
            ):
                raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
            rows.append(
                Market(
                    _text(row.get("market_id")),
                    exchange,
                    _text(row.get("question")),
                    status,
                    _optional_price(row.get("last_yes_price")),
                    _optional_price(row.get("last_no_price")),
                )
            )
        # Search has no total/has_more. A full page may have another page; only
        # a subsequent short/empty page establishes exhaustion.
        return OddpoolPage(
            tuple(rows),
            original,
            len(rows) == limit,
            next_offset=offset + limit if len(rows) == limit else None,
        )

    def historical_orderbook(
        self,
        *,
        market_id: str,
        start_time: int | None = None,
        end_time: int | None = None,
        granularity: str = "1m",
        limit: int = 100,
        pagination_key: str | None = None,
    ) -> OddpoolPage:
        if not isinstance(market_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", market_id):
            raise OddpoolError("ODDPOOL_MARKET_ID_INVALID")
        if granularity not in {"1m", "5m"} or type(limit) is not int or not 1 <= limit <= 200:
            raise OddpoolError("ODDPOOL_PAGE_BOUND_INVALID")
        if (start_time is None) != (end_time is None):
            raise OddpoolError("ODDPOOL_TIME_WINDOW_INVALID")
        params: dict[str, str | int] = dict(
            market_id=market_id, granularity=granularity, limit=limit
        )
        if start_time is not None and end_time is not None:
            if (
                type(start_time) is not int
                or type(end_time) is not int
                or not 0 <= start_time < end_time
            ):
                raise OddpoolError("ODDPOOL_TIME_WINDOW_INVALID")
            params.update(start_time=start_time, end_time=end_time)
        if pagination_key is not None:
            if not isinstance(pagination_key, str) or not 1 <= len(pagination_key) <= 4096:
                raise OddpoolError("ODDPOOL_CURSOR_INVALID")
            params["pagination_key"] = pagination_key
        data, original = self._get("/historical/kalshi/orderbook", params)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("snapshots"), list)
            or not isinstance(data.get("pagination"), dict)
        ):
            raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
        pagination = data["pagination"]
        items = data["snapshots"]
        more, cursor = pagination.get("has_more"), pagination.get("pagination_key")
        if len(items) > limit or pagination.get("count") != len(items) or type(more) is not bool:
            raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
        if more and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
            raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
        snapshots = []
        seen = set()
        for row in items:
            if not isinstance(row, dict) or row.get("market_id") != market_id:
                raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
            timestamp = row.get("timestamp")
            if type(timestamp) is not int or timestamp < 0 or timestamp in seen:
                raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
            if (
                start_time is not None
                and end_time is not None
                and not start_time <= timestamp <= end_time
            ):
                raise OddpoolError("ODDPOOL_SCHEMA_INVALID")
            seen.add(timestamp)
            snapshots.append(
                BookSnapshot(
                    market_id,
                    timestamp,
                    _levels(row.get("yes_bids")),
                    _levels(row.get("no_bids")),
                    _optional_price(row.get("best_yes_bid")),
                    _optional_price(row.get("best_yes_ask")),
                )
            )
        return OddpoolPage(tuple(snapshots), original, more, cursor if more else None)

    def historical_orderbook_slice(
        self,
        *,
        market_id: str,
        start_time: int,
        end_time: int,
        max_pages: int = 2,
        limit: int = 100,
    ) -> tuple[OddpoolPage, ...]:
        if type(max_pages) is not int or not 1 <= max_pages <= 5:
            raise OddpoolError("ODDPOOL_PAGE_BOUND_INVALID")
        pages = []
        cursor = None
        seen: set[str] = set()
        for _ in range(max_pages):
            page = self.historical_orderbook(
                market_id=market_id,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                pagination_key=cursor,
            )
            pages.append(page)
            if not page.has_more:
                break
            cursor = page.next_cursor
            if cursor is None or cursor in seen:
                raise OddpoolError("ODDPOOL_PAGINATION_CYCLE")
            seen.add(cursor)
        return tuple(pages)
