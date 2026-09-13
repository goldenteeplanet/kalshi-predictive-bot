import hashlib
from decimal import Decimal

import httpx
import pytest

from kalshi_predictor.research import oddpool
from kalshi_predictor.research.oddpool import BookSnapshot, OddpoolError, OddpoolResearchClient

KEY = "oddpool_synthetic_test_key"


@pytest.fixture(autouse=True)
def virtual_pacing(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(oddpool.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        oddpool.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    return clock


def book(more=False, cursor=None):
    return {
        "snapshots": [
            {
                "market_id": "KXTEST-YES",
                "timestamp": 2000,
                "yes_bids": [{"price": "0.30", "size": 2.5}],
                "no_bids": [{"price": "0.60", "size": 4}],
                "best_yes_bid": 0.30,
                "best_yes_ask": 0.40,
            }
        ],
        "pagination": {"count": 1, "has_more": more, "pagination_key": cursor},
    }


def test_search_schema_auth_and_original_provenance():
    requests = []
    raw = (
        b'[{"market_id":"KXTEST-YES","exchange":"kalshi","question":"Question?",'
        b'"status":"active","last_yes_price":"0.3000"}]'
    )

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    with OddpoolResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        page = client.search_markets(q="test")
        assert page.records[0].last_yes_price == Decimal("0.3000")
        assert page.original.payload == raw
        assert page.original.sha256 == hashlib.sha256(raw).hexdigest()
        assert page.original.received_at.tzinfo is not None
        assert KEY not in repr(client) + repr(page) + page.original.url
    request = requests[0]
    assert request.method == "GET" and request.url.host == "api.oddpool.com"
    assert request.url.path == "/search/markets" and request.url.params["q"] == "test"
    assert request.headers["X-API-Key"] == KEY
    assert request.extensions["timeout"] == dict(connect=10.0, read=10.0, write=10.0, pool=10.0)


def test_book_normalization_bounded_pages_pacing(virtual_pacing):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=book(True, str(len(requests))))

    with OddpoolResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        pages = client.historical_orderbook_slice(
            market_id="KXTEST-YES", start_time=1000, end_time=3000, max_pages=2
        )
        assert len(pages) == 2 and pages[-1].has_more
        row = pages[0].records[0]
        assert isinstance(row, BookSnapshot)
        assert row.yes_bids[0].size == Decimal("2.5")
        assert row.best_yes_ask == Decimal("0.40")
        assert client.remaining_requests == 8
    assert len(requests) == 2
    assert requests[1].url.params["pagination_key"] == "1"
    assert virtual_pacing[0] >= 101.05


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AUTHENTICATION_FAILED"),
        (403, "TIER_OR_ACCESS_DENIED"),
        (429, "RATE_OR_QUOTA_LIMITED"),
        (302, "REDIRECT_REJECTED"),
        (500, "HTTP_FAILURE"),
    ],
)
def test_error_responses_are_not_echoed_or_retried(status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text=KEY,
            headers={"location": "https://evil.invalid/" + KEY, "retry-after": "120"},
        )

    with OddpoolResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OddpoolError, match=code) as error:
            client.search_markets(q="test")
        assert KEY not in str(error.value) + repr(error.value)
        assert error.value.retry_after_seconds == 120
        assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b'[{"market_id":"missing-fields"}]',
        b'[{"market_id":"x","exchange":"kalshi","question":"x","status":"active","last_yes_price":"NaN"}]',
    ],
)
def test_malformed_search_failclosed(payload):
    with OddpoolResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=payload, headers={"content-type": "application/json"}
            )
        ),
    ) as client:
        with pytest.raises(OddpoolError, match="JSON_INVALID|SCHEMA_INVALID"):
            client.search_markets(q="test")


def test_quota_and_secret_queries_do_not_send():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    with OddpoolResearchClient(
        KEY, request_budget=1, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OddpoolError, match="SECRET_IN_QUERY"):
            client.search_markets(q=KEY)
        assert not requests
        client.search_markets(q="test")
        with pytest.raises(OddpoolError, match="QUOTA_EXHAUSTED"):
            client.search_markets(q="test")
        assert len(requests) == 1


@pytest.mark.parametrize("mutation", ["timestamp", "price", "size", "cursor", "count"])
def test_invalid_book_schema(mutation):
    data = book()
    if mutation == "timestamp":
        data["snapshots"][0]["timestamp"] = True
    if mutation == "price":
        data["snapshots"][0]["yes_bids"][0]["price"] = "1.1"
    if mutation == "size":
        data["snapshots"][0]["yes_bids"][0]["size"] = -1
    if mutation == "cursor":
        data["pagination"]["has_more"] = True
    if mutation == "count":
        data["pagination"]["count"] = 2
    with OddpoolResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as client:
        with pytest.raises(OddpoolError, match="SCHEMA_INVALID"):
            client.historical_orderbook(market_id="KXTEST-YES")


def test_size_limit_and_success_credential_echo_rejected():
    for body, expected in [
        (b"x" * (1_048_576 + 1), "RESPONSE_TOO_LARGE"),
        (('"' + KEY + '"').encode(), "CREDENTIAL_ECHO"),
    ]:
        with OddpoolResearchClient(
            KEY,
            transport=httpx.MockTransport(
                lambda r, body=body: httpx.Response(
                    200, content=body, headers={"content-type": "application/json"}
                )
            ),
        ) as client:
            with pytest.raises(OddpoolError, match=expected):
                client.search_markets(q="test")


def test_json_escaped_key_echo_is_rejected():
    body = ('"' + "".join(f"\\u{ord(char):04x}" for char in KEY) + '"').encode()
    with OddpoolResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        ),
    ) as client:
        with pytest.raises(OddpoolError, match="CREDENTIAL_ECHO"):
            client.search_markets(q="test")


def test_transport_message_redaction_and_no_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://unreachable.invalid:9999")

    def handler(request):
        raise httpx.ConnectError(KEY, request=request)

    with OddpoolResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        assert client._OddpoolResearchClient__client.trust_env is False
        assert client._OddpoolResearchClient__client.follow_redirects is False
        with pytest.raises(OddpoolError, match="TRANSPORT_FAILED") as error:
            client.search_markets(q="test")
        assert KEY not in str(error.value)
        assert error.value.__context__ is None
        assert error.value.__cause__ is None


def test_cursor_cycle_and_invalid_page_bound():
    with OddpoolResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=book(True, "same")))
    ) as client:
        with pytest.raises(OddpoolError, match="PAGINATION_CYCLE"):
            client.historical_orderbook_slice(
                market_id="KXTEST-YES", start_time=1000, end_time=3000
            )
        with pytest.raises(OddpoolError, match="PAGE_BOUND"):
            client.historical_orderbook_slice(
                market_id="KXTEST-YES", start_time=1000, end_time=3000, max_pages=6
            )
