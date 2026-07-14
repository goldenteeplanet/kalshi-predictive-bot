import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any

import httpx

from kalshi_predictor.config import Settings, get_settings

logger = logging.getLogger(__name__)

MarketPageCallback = Callable[[dict[str, Any]], None]

RATE_LIMITED_PARTIAL = "RATE_LIMITED_PARTIAL"
RATE_LIMITED_ABORTED = "RATE_LIMITED_ABORTED"
RATE_LIMITED_RETRY_EXHAUSTED = "RATE_LIMITED_RETRY_EXHAUSTED"

_GLOBAL_RATE_LIMIT_LOCK = Lock()
_GLOBAL_NEXT_REQUEST_MONOTONIC = 0.0


@dataclass
class KalshiEndpointRateLimitStat:
    endpoint: str
    status_code: int
    retry_count: int = 0
    total_sleep_seconds: float = 0.0
    retry_exhausted: bool = False


@dataclass
class KalshiRateLimitTelemetry:
    request_count: int = 0
    retry_count: int = 0
    total_sleep_seconds: float = 0.0
    rate_limited_count: int = 0
    retry_exhausted_count: int = 0
    endpoints: dict[str, KalshiEndpointRateLimitStat] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def record_request(self) -> None:
        self.request_count += 1

    def record_retry(
        self,
        *,
        endpoint: str,
        status_code: int,
        delay: float,
        retry_count: int,
        retry_exhausted: bool = False,
        retry_after_header: str | None = None,
    ) -> None:
        self.retry_count += 1
        self.total_sleep_seconds += delay
        if status_code == 429:
            self.rate_limited_count += 1
        if retry_exhausted:
            self.retry_exhausted_count += 1
        stat = self.endpoints.get(endpoint)
        if stat is None:
            stat = KalshiEndpointRateLimitStat(endpoint=endpoint, status_code=status_code)
            self.endpoints[endpoint] = stat
        stat.status_code = status_code
        stat.retry_count += 1
        stat.total_sleep_seconds += delay
        stat.retry_exhausted = stat.retry_exhausted or retry_exhausted
        self.events.append(
            {
                "endpoint": endpoint,
                "status_code": status_code,
                "retry_count": retry_count,
                "sleep_seconds": round(delay, 3),
                "retry_after_header": retry_after_header,
                "retry_exhausted": retry_exhausted,
            }
        )

    @property
    def rate_limited(self) -> bool:
        return self.rate_limited_count > 0

    @property
    def status(self) -> str:
        if self.retry_exhausted_count > 0:
            return RATE_LIMITED_RETRY_EXHAUSTED
        if self.rate_limited:
            return RATE_LIMITED_PARTIAL
        return "COMPLETE"

    def as_dict(self, *, rows_fetched_before_limit: int = 0) -> dict[str, Any]:
        endpoint_rows = [
            {
                "endpoint": stat.endpoint,
                "status_code": stat.status_code,
                "retry_count": stat.retry_count,
                "total_sleep_seconds": round(stat.total_sleep_seconds, 3),
                "retry_exhausted": stat.retry_exhausted,
            }
            for stat in sorted(self.endpoints.values(), key=lambda item: item.endpoint)
        ]
        return {
            "status": self.status,
            "rate_limited": self.rate_limited,
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "rate_limited_count": self.rate_limited_count,
            "retry_exhausted_count": self.retry_exhausted_count,
            "total_sleep_seconds": round(self.total_sleep_seconds, 3),
            "rows_fetched_before_limit": rows_fetched_before_limit,
            "data_completeness": "partial" if self.rate_limited else "complete",
            "endpoints": endpoint_rows,
            "events": list(self.events[-50:]),
        }


class KalshiClientError(RuntimeError):
    """Base exception for public Kalshi client failures."""


class KalshiAPIError(KalshiClientError):
    """Raised when Kalshi returns a non-retryable error response."""


class KalshiRetryError(KalshiClientError):
    """Raised after retryable requests exhaust all attempts."""


class KalshiClient:
    """Small synchronous client for Kalshi public GET endpoints only."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        max_command_retries: int | None = None,
        backoff_seconds: float | None = None,
        throttle_seconds: float | None = None,
        jitter_fraction: float = 0.2,
        sleeper: Callable[[float], None] | None = None,
        user_agent: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        resolved_settings = settings or get_settings()
        self.base_url = (base_url or resolved_settings.kalshi_base_url).rstrip("/")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else resolved_settings.kalshi_request_timeout_seconds
        )
        self.max_retries = (
            max_retries if max_retries is not None else resolved_settings.kalshi_max_retries
        )
        self.max_command_retries = (
            max_command_retries
            if max_command_retries is not None
            else resolved_settings.kalshi_max_command_retries
        )
        self.backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else resolved_settings.kalshi_retry_backoff_seconds
        )
        self.throttle_seconds = (
            throttle_seconds
            if throttle_seconds is not None
            else resolved_settings.kalshi_public_api_throttle_seconds
        )
        self.jitter_fraction = max(0.0, jitter_fraction)
        self._sleeper = sleeper or time.sleep
        self._command_retries_used = 0
        self.telemetry = KalshiRateLimitTelemetry()
        resolved_user_agent = user_agent or resolved_settings.kalshi_user_agent
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_seconds),
            headers={"User-Agent": resolved_user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def get_markets(
        self,
        status: str | None = "open",
        limit: int = 100,
        cursor: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)
        return self._get("/markets", params=params)

    def get_series(self, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        return self._get("/series", params=params)

    def iter_markets(
        self,
        status: str | None = "open",
        limit: int = 100,
        max_pages: int | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        start_cursor: str | None = None,
        deadline_monotonic: float | None = None,
        page_callback: MarketPageCallback | None = None,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = start_cursor
        pages_seen = 0

        while True:
            if max_pages is not None and pages_seen >= max_pages:
                _notify_page_callback(
                    page_callback,
                    {
                        "event": "stop",
                        "stop_reason": "max_pages",
                        "pages_seen": pages_seen,
                        "resume_cursor": cursor,
                    },
                )
                break

            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                _notify_page_callback(
                    page_callback,
                    {
                        "event": "stop",
                        "stop_reason": "deadline",
                        "pages_seen": pages_seen,
                        "resume_cursor": cursor,
                    },
                )
                break

            page = self.get_markets(
                status=status,
                limit=limit,
                cursor=cursor,
                series_ticker=series_ticker,
                event_ticker=event_ticker,
            )
            pages_seen += 1

            markets = page.get("markets", [])
            if not isinstance(markets, list):
                raise KalshiAPIError("Kalshi /markets response did not contain a market list.")

            next_cursor = page.get("cursor")
            resolved_next_cursor = (
                next_cursor if isinstance(next_cursor, str) and next_cursor else None
            )
            _notify_page_callback(
                page_callback,
                {
                    "event": "page",
                    "pages_seen": pages_seen,
                    "request_cursor": cursor,
                    "next_cursor": resolved_next_cursor,
                    "resume_cursor": resolved_next_cursor,
                    "markets_on_page": len(markets),
                    "has_more": resolved_next_cursor is not None,
                },
            )

            for market in markets:
                if isinstance(market, dict):
                    yield market

            cursor = resolved_next_cursor
            if cursor is None:
                break

    def get_market(self, ticker: str) -> dict[str, Any]:
        payload = self._get(f"/markets/{ticker}")
        market = payload.get("market")
        return market if isinstance(market, dict) else payload

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self._get(f"/markets/{ticker}/orderbook")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = self.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                self._throttle_before_request()
                self.telemetry.record_request()
                response = self._client.request(method, path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.max_retries or not self._has_command_retry_budget():
                    break
                self._sleep_before_retry(method, path, attempt, None, exc)
                continue

            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt >= self.max_retries or not self._has_command_retry_budget():
                    retry_after_header = response.headers.get("Retry-After")
                    self.telemetry.record_retry(
                        endpoint=_endpoint_key(method, path),
                        status_code=response.status_code,
                        delay=0.0,
                        retry_count=attempt,
                        retry_exhausted=True,
                        retry_after_header=retry_after_header,
                    )
                    raise KalshiRetryError(
                        f"Kalshi {method} {path} failed after {attempts} attempts: "
                        f"HTTP {response.status_code} {response.text[:300]}"
                    )
                self._sleep_before_retry(method, path, attempt, response, None)
                continue

            if response.is_error:
                raise KalshiAPIError(
                    f"Kalshi {method} {path} returned HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise KalshiAPIError(f"Kalshi {method} {path} returned invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise KalshiAPIError(f"Kalshi {method} {path} returned a non-object JSON payload.")
            return payload

        raise KalshiRetryError(
            f"Kalshi {method} {path} failed after {attempts} attempts: {last_error}"
        )

    def _sleep_before_retry(
        self,
        method: str,
        path: str,
        attempt: int,
        response: httpx.Response | None,
        error: Exception | None,
    ) -> None:
        retry_after_header = response.headers.get("Retry-After") if response else None
        retry_after = _parse_retry_after(retry_after_header)
        delay = retry_after if retry_after is not None else _backoff_delay(
            self.backoff_seconds,
            attempt,
            jitter_fraction=self.jitter_fraction,
        )
        self._command_retries_used += 1
        if response is not None:
            self.telemetry.record_retry(
                endpoint=_endpoint_key(method, path),
                status_code=response.status_code,
                delay=delay,
                retry_count=attempt + 1,
                retry_after_header=retry_after_header,
            )
        if response is not None:
            logger.warning(
                "Retrying Kalshi request after HTTP %s in %.2fs",
                response.status_code,
                delay,
            )
        else:
            logger.warning("Retrying Kalshi request after %s in %.2fs", error, delay)
        self._sleeper(delay)

    def _has_command_retry_budget(self) -> bool:
        return self._command_retries_used < max(0, self.max_command_retries)

    def _throttle_before_request(self) -> None:
        global _GLOBAL_NEXT_REQUEST_MONOTONIC
        delay = max(0.0, self.throttle_seconds)
        if delay <= 0:
            return
        with _GLOBAL_RATE_LIMIT_LOCK:
            now = time.monotonic()
            sleep_for = max(0.0, _GLOBAL_NEXT_REQUEST_MONOTONIC - now)
            _GLOBAL_NEXT_REQUEST_MONOTONIC = max(now, _GLOBAL_NEXT_REQUEST_MONOTONIC) + delay
        if sleep_for > 0:
            self._sleeper(sleep_for)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, parsed.timestamp() - time.time())


def _backoff_delay(base_seconds: float, attempt: int, *, jitter_fraction: float) -> float:
    deterministic = min(30.0, max(0.0, base_seconds) * (2**attempt))
    if deterministic <= 0 or jitter_fraction <= 0:
        return deterministic
    return min(30.0, deterministic + random.uniform(0.0, deterministic * jitter_fraction))


def _endpoint_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def _notify_page_callback(
    callback: MarketPageCallback | None,
    payload: dict[str, Any],
) -> None:
    if callback is not None:
        callback(payload)
