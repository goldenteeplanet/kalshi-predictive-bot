import asyncio
import json

import pytest
from sqlalchemy import func, select
from test_gh1_websocket_orderbooks import (
    AUTH,
    TICKER,
    _FakeConnection,
    _FakeContext,
    _FakeRestClient,
    _snapshot,
)

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.ingest.websocket_orderbooks import (
    ReadOnlyOrderbookWebSocketAdapter,
    drain_staged_websocket_orderbooks,
)
from kalshi_predictor.kalshi.data_environment import endpoint_environment, matched_environment

PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


def test_observed_demo_stream_production_rest_refused_before_connect(tmp_path):
    with pytest.raises(ValueError, match="ENVIRONMENT_MISMATCH"):
        ReadOnlyOrderbookWebSocketAdapter(
            tickers=[TICKER], auth_headers=AUTH, staging_dir=tmp_path, rest_client=_FakeRestClient()
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "https://external-api.kalshi.com.attacker.test/trade-api/v2",
        "http://external-api.kalshi.com/trade-api/v2",
        "https://user:secret@external-api.kalshi.com/trade-api/v2",
        "https://external-api.kalshi.com/trade-api/v2?demo=true",
    ],
)
def test_untrusted_endpoint_refused(url):
    with pytest.raises(ValueError):
        endpoint_environment(url)


def test_documented_aliases_match():
    assert (
        matched_environment("https://api.elections.kalshi.com/trade-api/v2", PROD) == "production"
    )
    assert (
        matched_environment(
            "https://external-api.demo.kalshi.co/trade-api/v2",
            "wss://demo-api.kalshi.co/trade-api/ws/v2",
        )
        == "demo"
    )


@pytest.mark.parametrize("damage", ["legacy", "demo", "forged"])
def test_drain_retains_untrusted_original_without_inserting(tmp_path, damage):
    stage = tmp_path / "stage"
    adapter = ReadOnlyOrderbookWebSocketAdapter(
        tickers=[TICKER],
        auth_headers=AUTH,
        staging_dir=stage,
        rest_client=_FakeRestClient(),
        ws_url=PROD,
        connector=lambda *a, **kw: _FakeContext(_FakeConnection([json.dumps(_snapshot(seq=1))])),
    )
    asyncio.run(adapter.run(max_messages=1))
    path = next(stage.glob("*.json"))
    payload = json.loads(path.read_text())
    assert payload["source_environment"] == "production"
    if damage == "legacy":
        del payload["rest_base_url"]
    elif damage == "demo":
        payload.update(
            source_environment="demo",
            rest_base_url="https://external-api.demo.kalshi.co/trade-api/v2",
            websocket_url="wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        )
    else:
        payload["source_environment"] = "demo"
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'test.db'}"))
    drained = drain_staged_websocket_orderbooks(
        session_factory=factory,
        staging_dir=stage,
        writer_monitor_fn=lambda: {"safe_to_start_write": True},
    )
    assert drained["snapshots_inserted"] == 0 and drained["errors"]
    assert not path.exists()
    assert len(drained["quarantined_files"]) == 1
    from pathlib import Path

    assert Path(drained["quarantined_files"][0]).read_bytes() == original
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0
