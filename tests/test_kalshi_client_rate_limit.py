import httpx
import pytest

from kalshi_predictor.kalshi.client import (
    RATE_LIMITED_PARTIAL,
    RATE_LIMITED_RETRY_EXHAUSTED,
    KalshiClient,
    KalshiRetryError,
)


def test_kalshi_client_honors_retry_after_header() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"error": "rate limited"},
            )
        return httpx.Response(200, json={"markets": []})

    sleeps: list[float] = []
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        base_url="https://kalshi.example/trade-api/v2",
    )
    client = KalshiClient(
        client=http_client,
        max_retries=3,
        max_command_retries=3,
        throttle_seconds=0,
        jitter_fraction=0,
        sleeper=sleeps.append,
    )

    assert client.get_markets()["markets"] == []
    details = client.telemetry.as_dict(rows_fetched_before_limit=0)

    assert sleeps == [2.0]
    assert details["status"] == RATE_LIMITED_PARTIAL
    assert details["rate_limited"] is True
    assert details["total_sleep_seconds"] == 2.0
    assert details["endpoints"] == [
        {
            "endpoint": "GET /markets",
            "status_code": 429,
            "retry_count": 1,
            "total_sleep_seconds": 2.0,
            "retry_exhausted": False,
        }
    ]


def test_kalshi_client_uses_exponential_backoff_without_retry_after() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"markets": []})

    sleeps: list[float] = []
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        base_url="https://kalshi.example/trade-api/v2",
    )
    client = KalshiClient(
        client=http_client,
        max_retries=3,
        max_command_retries=3,
        backoff_seconds=1,
        throttle_seconds=0,
        jitter_fraction=0,
        sleeper=sleeps.append,
    )

    assert client.get_markets()["markets"] == []
    details = client.telemetry.as_dict(rows_fetched_before_limit=0)

    assert sleeps == [1.0, 2.0, 4.0]
    assert details["retry_count"] == 3
    assert details["total_sleep_seconds"] == 7.0
    assert details["status"] == RATE_LIMITED_PARTIAL


def test_kalshi_client_stops_when_command_retry_budget_is_exhausted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    sleeps: list[float] = []
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        base_url="https://kalshi.example/trade-api/v2",
    )
    client = KalshiClient(
        client=http_client,
        max_retries=5,
        max_command_retries=2,
        backoff_seconds=1,
        throttle_seconds=0,
        jitter_fraction=0,
        sleeper=sleeps.append,
    )

    with pytest.raises(KalshiRetryError):
        client.get_markets()
    details = client.telemetry.as_dict(rows_fetched_before_limit=0)

    assert sleeps == [1.0, 2.0]
    assert details["status"] == RATE_LIMITED_RETRY_EXHAUSTED
    assert details["retry_exhausted_count"] == 1
    assert details["endpoints"][0]["retry_exhausted"] is True
