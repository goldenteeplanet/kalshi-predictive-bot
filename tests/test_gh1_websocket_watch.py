from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.ingest.websocket_orderbooks import StreamSummary
from kalshi_predictor.ingest.websocket_watch import (
    discover_quoted_market_tickers,
    load_actionable_tickers,
    run_reconnecting_websocket_watch,
)


def test_discovery_keeps_bounded_quoted_books_per_series() -> None:
    client = _FakeClient()

    rows = discover_quoted_market_tickers(
        client=client,
        series=["KXBTC", "KXTEMPNYCH"],
        max_markets_per_series=5,
        max_quoted_per_series=1,
    )

    assert [row["ticker"] for row in rows] == ["KXBTC-QUOTED", "KXTEMPNYCH-QUOTED"]
    assert client.orderbook_calls == [
        "KXBTC-EMPTY",
        "KXBTC-QUOTED",
        "KXTEMPNYCH-EMPTY",
        "KXTEMPNYCH-QUOTED",
    ]


def test_discovery_prioritizes_ranked_manifest_books(tmp_path: Path) -> None:
    manifest = tmp_path / "actionable.json"
    manifest.write_text(
        json.dumps({"tickers": ["KXBTC-RANKED", "KXBTC-RANKED", "KXETH-RANKED"]}),
        encoding="utf-8",
    )
    tickers = load_actionable_tickers(manifest, limit=2)
    client = _FakeClient()

    rows = discover_quoted_market_tickers(
        client=client,
        series=["KXBTC", "KXETH"],
        max_markets_per_series=5,
        max_quoted_per_series=1,
        preferred_tickers=tickers,
    )

    assert [row["ticker"] for row in rows] == ["KXBTC-RANKED", "KXETH-RANKED"]
    assert all(row["selection_source"] == "ACTIONABLE_RANKING" for row in rows)
    assert client.orderbook_calls == ["KXBTC-RANKED", "KXETH-RANKED"]


def test_discovery_retains_preferred_ticker_with_empty_initial_book() -> None:
    client = _FakeClient()

    rows = discover_quoted_market_tickers(
        client=client,
        series=["KXXRP"],
        max_markets_per_series=5,
        max_quoted_per_series=1,
        preferred_tickers=["KXXRP-EMPTY"],
    )

    assert [row["ticker"] for row in rows] == ["KXXRP-EMPTY"]
    assert rows[0]["selection_source"] == "PREFERRED_SNAPSHOT_RECOVERY"
    assert rows[0]["yes_levels"] == 0
    assert rows[0]["no_levels"] == 0
    assert client.orderbook_calls == ["KXXRP-EMPTY"]


def test_watch_reconnects_uses_cached_discovery_and_stages_only(tmp_path: Path) -> None:
    adapter_factory = _AdapterFactory()
    sleeps: list[float] = []
    status_path = tmp_path / "watch" / "status.json"
    settings = Settings(
        kalshi_websocket_enabled=True,
        kalshi_websocket_staging_dir=str(tmp_path / "staging"),
        execution_enabled=True,
        execution_dry_run=False,
    )

    result = run_reconnecting_websocket_watch(
        settings=settings,
        series=["KXBTC"],
        max_markets_per_series=5,
        max_quoted_per_series=1,
        stream_max_seconds=1,
        discovery_refresh_seconds=600,
        healthy_cycle_delay_seconds=0,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=4,
        status_path=status_path,
        max_cycles=2,
        client_factory=_FakeClient,
        adapter_factory=adapter_factory,
        sleep_fn=sleeps.append,
    )

    assert result["state"] == "STOPPED_MAX_CYCLES"
    assert result["cycles_started"] == 2
    assert result["stream_cycles_completed"] == 1
    assert result["reconnect_count"] == 1
    assert result["discovery_refreshes"] == 1
    assert result["selected_tickers"] == ["KXBTC-QUOTED"]
    assert result["messages_seen"] == 3
    assert result["staged_files"] == 1
    assert result["consecutive_failures"] == 0
    assert result["last_discovery_success_at"] is not None
    assert result["last_stream_success_at"] is not None
    assert result["last_message_at"] is not None
    assert result["last_snapshot_at"] is not None
    assert result["safety"]["filesystem_stage_only"] is True
    assert result["safety"]["database_writes"] == 0
    assert result["safety"]["orders_submitted"] == 0
    assert sleeps == [1]
    assert adapter_factory.ticker_sets == [("KXBTC-QUOTED",), ("KXBTC-QUOTED",)]
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "STOPPED_MAX_CYCLES"
    assert persisted["safety"]["execution_enabled"] is False


class _FakeClient:
    def __init__(self, **_: object) -> None:
        self.orderbook_calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_markets(self, *, series_ticker: str, **_: object) -> dict[str, object]:
        return {
            "markets": [
                {"ticker": f"{series_ticker}-EMPTY"},
                {"ticker": f"{series_ticker}-QUOTED"},
            ]
        }

    def get_orderbook(self, ticker: str) -> dict[str, object]:
        self.orderbook_calls.append(ticker)
        if ticker.endswith("-EMPTY"):
            return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        return {"orderbook_fp": {"yes_dollars": [["0.40", "10"]], "no_dollars": []}}


class _FakeAdapter:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    async def run(self, **_: object) -> StreamSummary:
        if self.fail:
            raise ConnectionError("simulated disconnect")
        return StreamSummary(
            messages_seen=3,
            snapshots_seen=1,
            deltas_applied=2,
            sequence_recoveries=0,
            staged_files=("staged.json",),
            errors=(),
            timed_out=True,
        )


class _AdapterFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.ticker_sets: list[tuple[str, ...]] = []

    def __call__(self, *, tickers, **_: object) -> _FakeAdapter:
        self.calls += 1
        self.ticker_sets.append(tuple(tickers))
        return _FakeAdapter(fail=self.calls == 1)
