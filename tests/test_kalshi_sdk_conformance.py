import ast
import json
from pathlib import Path

import httpx
import pytest

from kalshi_predictor.conformance.kalshi_sdk import (
    ConformanceFixtureError,
    assert_public_read_only_fixture,
    compare_orderbook_delta,
    compare_orderbook_snapshot,
    fixture_manifest,
)
from kalshi_predictor.kalshi.client import KalshiClient

FIXTURE = Path("tests/fixtures/kalshi_sdk/public_orderbook_stream.json")
HARNESS = Path("src/kalshi_predictor/conformance/kalshi_sdk.py")


def test_fixture_guard_rejects_credentials_and_private_or_mutating_requests() -> None:
    with pytest.raises(ConformanceFixtureError, match="Sensitive"):
        assert_public_read_only_fixture({"headers": {"KALSHI-ACCESS-KEY": "secret"}})
    with pytest.raises(ConformanceFixtureError, match="Private"):
        assert_public_read_only_fixture({"request": {"method": "GET", "path": "/portfolio"}})
    with pytest.raises(ConformanceFixtureError, match="Mutating"):
        assert_public_read_only_fixture({"request": {"method": "POST", "path": "/markets"}})


def test_public_fixture_has_custody_manifest() -> None:
    manifest = fixture_manifest(FIXTURE)
    assert manifest["access"] == "PUBLIC_READ_ONLY"
    assert len(manifest["sha256"]) == 64
    assert manifest["sdk_version"] == "10.0.0"


def test_harness_imports_only_allowlisted_public_sdk_models() -> None:
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    sdk_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and (node.module == "kalshi" or node.module.startswith("kalshi."))
    }
    assert sdk_imports == {"kalshi.ws.models.orderbook_delta"}


def test_public_market_pagination_preserves_cursor_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"markets": [{"ticker": "FIRST"}], "cursor": "NEXT"})
        assert cursor == "NEXT"
        return httpx.Response(200, json={"markets": [{"ticker": "SECOND"}], "cursor": ""})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.elections.kalshi.com") as http:
        client = KalshiClient(client=http, throttle_seconds=0)
        markets = list(client.iter_markets(limit=1))

    assert [market["ticker"] for market in markets] == ["FIRST", "SECOND"]
    assert [request.url.params.get("cursor") for request in requests] == [None, "NEXT"]


def test_sdk_and_local_orderbook_snapshot_and_delta_are_conformant() -> None:
    pytest.importorskip("kalshi")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot = compare_orderbook_snapshot(payload["snapshot"])
    delta = compare_orderbook_delta(payload["snapshot"], payload["delta"])

    assert snapshot["best_yes_bid"] == "0.4200"
    assert snapshot["best_yes_ask"] == "0.4300"
    assert delta["sequence"] == 102
    assert delta["quantity_after"] == "15.00"
