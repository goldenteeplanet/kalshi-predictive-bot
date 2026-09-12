"""Offline provider boundary and real JSON-envelope tests; no actual API key."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from kalshi_predictor.research import unusual_whales as uw

KEY = "synthetic-test-token-never-a-real-key"
ASSET = "78699665722135183571140787397681621204678465342167575193369860798796866652931"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(uw.time, "sleep", lambda _: None)


def client(handler, **kwargs):
    return uw.UnusualWhalesResearchClient(KEY, transport=httpx.MockTransport(handler), **kwargs)


def unusual():
    return {
        "data": {
            "categories": ["Finance"],
            "data": [
                {
                    "asset_id": ASSET,
                    "market": "Synthetic policy event?",
                    "category": "Finance",
                    "current": "0.6200",
                    "outcome": "Yes",
                    "resolves": "2026-09-09T00:00:00Z",
                    "smart_volume": "340000",
                    "unusual_score": "4.65",
                    "volume": "1250000.50",
                }
            ],
        }
    }


def test_unusual_original_nested_shape_auth_and_conservative_provenance():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.unusualwhales.com"
        assert request.url.path == "/api/predictions/unusual"
        assert dict(request.url.params) == {"limit": "5", "offset": "0", "categories": "Finance"}
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(200, json=unusual())

    with client(handler) as provider:
        result = provider.unusual_markets(limit=5, categories="Finance")
        assert KEY not in repr(provider) + repr(result) + repr(result.original)
        assert provider.remaining_requests == 9
    assert len(requests) == 1
    assert result.data["data"][0]["asset_id"] == ASSET
    assert hashlib.sha256(result.original.payload).hexdigest() == result.original.sha256
    assert json.loads(result.original.payload) == unusual()
    assert result.original.received_at.tzinfo is UTC
    assert result.provider_timestamp is None
    assert result.original.research_only
    assert not result.original.runtime_certified
    assert not result.original.venue_mapping_verified


def test_details_identity_and_liquidity_clock_is_not_receipt():
    stamp = "2026-09-01T18:32:11Z"

    def handler(request):
        if request.url.path.endswith("/liquidity"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "asset_id": ASSET,
                        "timestamp": stamp,
                        "bids": [{"price": "0.6101", "size": "12.5"}],
                        "asks": [{"price": "0.63", "size": "100"}],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "asset_id": ASSET,
                    "active": True,
                    "end_date": "2026-09-09T00:00:00Z",
                    "question": "Synthetic?",
                }
            },
        )

    with client(handler) as provider:
        details = provider.market_details(asset_id=ASSET)
        result = provider.market_liquidity(asset_id=ASSET)
    assert details.provider_timestamp is None
    assert result.provider_timestamp == datetime(2026, 9, 1, 18, 32, 11, tzinfo=UTC)
    assert result.original.received_at != result.provider_timestamp
    assert result.data["bids"][0]["size"] == "12.5"


def test_calendar_event_clock_does_not_assert_provider_publication():
    body = {
        "data": [
            {
                "event": "PCE index",
                "time": "2026-09-25T12:30:00Z",
                "forecast": None,
                "prev": "0.0%",
                "type": "report",
            }
        ]
    }
    with client(lambda _: httpx.Response(200, json=body)) as provider:
        result = provider.economic_calendar()
    assert result.data == body["data"]
    assert result.provider_timestamp is None


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AUTHENTICATION_FAILED"),
        (403, "ACCESS_OR_TIER_DENIED"),
        (429, "RATE_OR_QUOTA_LIMITED"),
        (302, "REDIRECT_REJECTED"),
    ],
)
def test_auth_quota_redirect_halt_without_retry_or_secret_echo(status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text=KEY,
            headers={"location": "https://example.invalid/" + KEY, "retry-after": "120"},
        )

    with client(handler) as provider:
        with pytest.raises(uw.UnusualWhalesError, match=code) as failure:
            provider.unusual_markets()
        assert failure.value.retry_after_seconds == 120
        assert KEY not in str(failure.value) + repr(failure.value)
        with pytest.raises(uw.UnusualWhalesError, match="CLIENT_HALTED"):
            provider.economic_calendar()
    assert len(requests) == 1


def test_transport_error_never_exposes_original_http_exception(capsys):
    def handler(request):
        raise httpx.ReadTimeout(KEY, request=request)

    with client(handler) as provider:
        with pytest.raises(uw.UnusualWhalesError, match="TRANSPORT_FAILED") as failure:
            provider.unusual_markets()
    assert str(failure.value) == "UW_TRANSPORT_FAILED"
    assert failure.value.__context__ is None
    assert KEY not in capsys.readouterr().out


@pytest.mark.parametrize(
    "body,code",
    [
        (b'{"data": [], "data": []}', "JSON_INVALID"),
        (b'{"data": NaN}', "JSON_INVALID"),
        (b"<html>bad</html>", "JSON_INVALID"),
        (b'{"data": {"data": [null]}}', "SCHEMA_INVALID"),
        (b'{"data": {"data": [{}]}}', "ASSET_ID_INVALID"),
        (json.dumps({"data": KEY}).encode(), "CREDENTIAL_ECHO_REJECTED"),
        (
            b'{"data":"' + b"".join(f"\\u{ord(c):04x}".encode() for c in KEY) + b'"}',
            "CREDENTIAL_ECHO_REJECTED",
        ),
    ],
)
def test_malformed_and_echoed_payloads_do_not_escape(body, code):
    with client(
        lambda _: httpx.Response(200, content=body, headers={"content-type": "application/json"})
    ) as provider:
        with pytest.raises(uw.UnusualWhalesError, match=code):
            provider.unusual_markets()


def test_response_bytes_bound_and_no_encoded_payloads():
    with client(
        lambda _: httpx.Response(
            200, content=b"x" * (uw._MAX_BYTES + 1), headers={"content-type": "application/json"}
        )
    ) as provider:
        with pytest.raises(uw.UnusualWhalesError, match="RESPONSE_TOO_LARGE"):
            provider.unusual_markets()
    with client(
        lambda _: httpx.Response(200, json=unusual(), headers={"content-encoding": "gzip"})
    ) as provider:
        with pytest.raises(uw.UnusualWhalesError):
            provider.unusual_markets()


@pytest.mark.parametrize(
    "asset", ["../users", "x/positions", "https://evil.invalid", "x%2fusers", ""]
)
def test_asset_paths_reject_before_network(asset):
    with client(lambda _: pytest.fail("unexpected network")) as provider:
        with pytest.raises(uw.UnusualWhalesError, match="ASSET_ID_INVALID"):
            provider.market_details(asset_id=asset)


def test_private_endpoints_query_injection_and_local_budget():
    with client(lambda _: httpx.Response(200, json=unusual()), request_budget=1) as provider:
        for path in (
            "/api/predictions/user/a",
            "/api/predictions/market/a/positions",
            "https://evil.invalid",
            "//evil.invalid",
        ):
            with pytest.raises(uw.UnusualWhalesError, match="ENDPOINT_NOT_ALLOWED"):
                provider._get(path, {})
        with pytest.raises(uw.UnusualWhalesError, match="QUERY_NOT_ALLOWED"):
            provider._get(uw._UNUSUAL, {"token": KEY})
        with pytest.raises(uw.UnusualWhalesError, match="SECRET_IN_URL"):
            provider.market_details(asset_id=KEY)
        provider.unusual_markets()
        with pytest.raises(uw.UnusualWhalesError, match="LOCAL_QUOTA_EXHAUSTED"):
            provider.unusual_markets()


def test_pacing_and_closed_state(monkeypatch):
    times = iter((0.0, 0.0, 0.0, 0.1, 1.05, 1.05))
    sleeps = []
    monkeypatch.setattr(uw.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(uw.time, "sleep", sleeps.append)
    with client(lambda _: httpx.Response(200, json=unusual())) as provider:
        provider.unusual_markets()
        provider.unusual_markets()
    assert sleeps == [pytest.approx(0.95)]
    with pytest.raises(uw.UnusualWhalesError, match="CLIENT_CLOSED"):
        provider.unusual_markets()


def test_precise_json_numbers_and_missing_liquidity_timestamp():
    raw = b'{"data":{"asset_id":"123","bids":[],"asks":[],"mid_price":0.123456789123456789}}'
    with client(
        lambda _: httpx.Response(200, content=raw, headers={"content-type": "application/json"})
    ) as provider:
        result = provider.market_liquidity(asset_id="123")
    assert result.data["mid_price"] == Decimal("0.123456789123456789")
    assert result.provider_timestamp is None
