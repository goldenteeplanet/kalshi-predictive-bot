import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from kalshi_predictor.overnight_paper.discovery import (
    PublicArchive,
    candidate_row,
    run_discovery,
    settlement_bucket,
)


@pytest.mark.parametrize(
    "hours,bucket",
    [
        (0, None),
        (1, "<=1h"),
        (1.01, "1-3h"),
        (3.01, "3-6h"),
        (6.01, "6-12h"),
        (12.01, "12-24h"),
        (24.01, "24-48h"),
        (48.01, "48-72h"),
        (72.01, None),
    ],
)
def test_settlement_buckets(hours, bucket):
    assert settlement_bucket(hours) == bucket


@pytest.mark.parametrize(
    "path",
    [
        "/portfolio/orders",
        "/balance",
        "/orders",
        "https://evil.example/markets",
        "/markets/../orders",
    ],
)
def test_public_boundary_rejects_account_paths(tmp_path, path):
    public = PublicArchive(tmp_path / "new")
    with pytest.raises(ValueError):
        public.get(path)
    assert public.receipts == []


def test_existing_archive_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        PublicArchive(tmp_path)


def test_rate_limit_stops_capture_without_endpoint_fallback(tmp_path, monkeypatch):
    from kalshi_predictor.overnight_paper import discovery

    calls = []
    original_client = httpx.Client

    def respond(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "rate limit"}, headers={"Retry-After": "60"})

    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    public = PublicArchive(tmp_path / "new")
    with pytest.raises(httpx.HTTPStatusError):
        public.get("/markets")
    for path in ("/markets/TICKER/orderbook", "/products/BTC-USD/candles"):
        with pytest.raises(RuntimeError, match="PUBLIC_RATE_LIMITED_CAPTURE_STOPPED"):
            public.get(path)
    assert len(calls) == 1
    assert public.receipts[0]["retry_after"] == "60"
    assert json.loads((public.root / "requests.json").read_text())[0]["status"] == 429


def test_public_requests_are_paced_and_deadline_prevents_next_call(tmp_path, monkeypatch):
    from kalshi_predictor.overnight_paper import discovery

    clock = [100.0]
    calls = []
    original_client = httpx.Client
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        discovery.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )

    def respond(request):
        calls.append(clock[0])
        return httpx.Response(200, json={"markets": []})

    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    public = PublicArchive(tmp_path / "new", seconds=1)
    public.get("/markets")
    public.get("/markets")
    with pytest.raises(RuntimeError, match="PUBLIC_REQUEST_BUDGET_EXHAUSTED"):
        public.get("/markets")
    assert calls == [100.0, 100.5]


def test_discovery_candidate_never_claims_ready_from_market_text():
    raw = {
        "ticker": "KXBTC-EXAMPLE-T60000",
        "event_ticker": "KXBTC-EXAMPLE",
        "title": "Bitcoin above $60000?",
        "expected_expiration_time": "2026-09-08T01:00Z",
    }
    row = candidate_row(
        raw, {"ticker": "KXBTC", "category": "Crypto"}, datetime(2026, 9, 8, tzinfo=UTC)
    )
    assert row["forecast"] is None
    assert row["net_ev"] is None
    assert row["settlement_rule_status"] == "UNCERTIFIED"
    assert row["paper_readiness"] == "PAPER_NOT_READY"


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_expiration_time": None},
        {"expected_expiration_time": "malformed"},
        {"latest_expiration_time": "malformed"},
    ],
)
def test_malformed_required_candidate_clocks_fail_closed(overrides):
    raw = {"ticker": "KXBTC-T1", "expected_expiration_time": "2026-09-08T01:00:00Z"}
    with pytest.raises(ValueError, match="MISSING_OR_INVALID_TIMESTAMP"):
        candidate_row(
            raw | overrides,
            {"ticker": "KXBTC", "category": "Crypto"},
            datetime(2026, 9, 8, tzinfo=UTC),
        )


def test_resume_preserves_cursor_and_frozen_window(tmp_path, monkeypatch):
    prior = tmp_path / "prior"
    prior.mkdir()
    data = b'{"markets": []}'
    response = prior / "page.json"
    response.write_bytes(data)
    (prior / "requests.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://external-api.kalshi.com/trade-api/v2/markets",
                    "status": 200,
                    "path": str(response),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )
    (prior / "universe.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-09-08T00:00:00+00:00",
                "coverage": {"resume_cursor": "cursor-1", "pagination_complete": False, "pages": 3},
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_get(self, path, params=None):
        calls.append((path, params))
        return {"series": [], "markets": [], "cursor": ""}

    monkeypatch.setattr(PublicArchive, "get", fake_get)
    result = run_discovery(tmp_path / "new", resume_from=prior)
    query = calls[1][1]
    assert query["cursor"] == "cursor-1"
    assert query["min_close_ts"] == int(datetime(2026, 9, 8, tzinfo=UTC).timestamp())
    assert "status" not in query  # API forbids open + close-timestamp filters.
    assert result["coverage"]["pages"] == 4
    assert result["coverage"]["pagination_complete"]
