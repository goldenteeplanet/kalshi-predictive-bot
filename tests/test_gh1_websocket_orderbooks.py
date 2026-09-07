from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.ingest.websocket_orderbooks import (
    ReadOnlyOrderbookWebSocketAdapter,
    drain_staged_websocket_orderbooks,
)
from kalshi_predictor.kalshi.orderbook import LocalOrderbook, OrderbookSequenceGap

TICKER = "KXTEMPNYCH-26JUL1514-T88.99"
AUTH = {
    "KALSHI-ACCESS-KEY": "test-key",
    "KALSHI-ACCESS-SIGNATURE": "test-signature",
    "KALSHI-ACCESS-TIMESTAMP": "1234567890000",
}


def test_local_orderbook_reconstructs_and_calculates_liquidity() -> None:
    book = LocalOrderbook(TICKER)
    book.apply_snapshot(_snapshot(seq=10))
    book.apply_delta(_delta(seq=11, side="yes", price="0.44", delta="3"))
    book.apply_delta(_delta(seq=12, side="no", price="0.53", delta="-2"))

    assert book.best_yes_bid == Decimal("0.44")
    assert book.best_yes_ask == Decimal("0.47")
    assert book.spread == Decimal("0.03")
    assert book.depth(side="yes", levels=5) == Decimal("13")
    assert book.imbalance == Decimal("5") / Decimal("21")
    quote = book.execution_quote(outcome="yes", action="buy", size="5")
    assert quote.fully_executable is True
    assert quote.average_price == Decimal("0.47")


def test_local_orderbook_detects_sequence_gap() -> None:
    book = LocalOrderbook(TICKER)
    book.apply_snapshot(_snapshot(seq=20))

    try:
        book.apply_delta(_delta(seq=22, side="yes", price="0.45", delta="1"))
    except OrderbookSequenceGap as exc:
        assert exc.expected == 21
        assert exc.actual == 22
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected a sequence gap")


def test_adapter_recovers_gap_stages_snapshots_and_never_sends_orders(tmp_path: Path) -> None:
    connection = _FakeConnection(
        [
            json.dumps(_snapshot(seq=1)),
            json.dumps(_delta(seq=3, side="yes", price="0.45", delta="1")),
        ]
    )
    rest = _FakeRestClient()
    adapter = ReadOnlyOrderbookWebSocketAdapter(
        tickers=[TICKER],
        auth_headers=AUTH,
        staging_dir=tmp_path,
        rest_client=rest,  # type: ignore[arg-type]
        connector=lambda *_args, **_kwargs: _FakeContext(connection),
        persist_every_deltas=1,
    )

    summary = asyncio.run(adapter.run(max_messages=2))

    assert summary.snapshots_seen == 1
    assert summary.sequence_recoveries == 1
    assert adapter.books[TICKER].sequence == 3
    assert adapter.books[TICKER].recovery_count == 1
    assert len(summary.staged_files) == 2
    sent = [json.loads(message) for message in connection.sent]
    assert [message["cmd"] for message in sent] == ["subscribe"]
    assert sent[0]["params"]["channels"] == ["orderbook_delta"]
    assert all("order" not in message for message in sent)
    staged = json.loads(Path(summary.staged_files[-1]).read_text(encoding="utf-8"))
    assert staged["safety"]["execution_enabled"] is False
    assert staged["orderbook"]["gh1_local_orderbook"]["sequence"] == 3


def test_guarded_drain_blocks_then_persists_with_one_writer(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    connection = _FakeConnection([json.dumps(_snapshot(seq=1))])
    adapter = ReadOnlyOrderbookWebSocketAdapter(
        tickers=[TICKER],
        auth_headers=AUTH,
        staging_dir=staging_dir,
        rest_client=_FakeRestClient(),  # type: ignore[arg-type]
        connector=lambda *_args, **_kwargs: _FakeContext(connection),
    )
    asyncio.run(adapter.run(max_messages=1))
    engine = init_db(f"sqlite:///{tmp_path / 'gh1.db'}")
    session_factory = get_session_factory(engine)

    blocked = drain_staged_websocket_orderbooks(
        session_factory=session_factory,
        staging_dir=staging_dir,
        writer_monitor_fn=lambda: {"safe_to_start_write": False, "current_writer_pid": 42},
    )
    assert blocked["status"] == "BLOCKED_ACTIVE_WRITER"
    assert blocked["snapshots_inserted"] == 0

    completed = drain_staged_websocket_orderbooks(
        session_factory=session_factory,
        staging_dir=staging_dir,
        writer_monitor_fn=lambda: {"safe_to_start_write": True, "current_writer_pid": None},
    )
    assert completed["status"] == "COMPLETE"
    assert completed["snapshots_inserted"] == 1
    assert completed["files_archived"] == 1
    assert completed["single_writer_session_count"] == 1
    assert completed["execution_enabled"] is False
    assert list(staging_dir.glob("*.json")) == []
    assert len(list((staging_dir / "drained").glob("*.json"))) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 1
        snapshot = session.scalar(select(MarketSnapshot))
        assert snapshot is not None
        assert snapshot.ticker == TICKER
        payload = json.loads(snapshot.raw_orderbook_json or "{}")
        assert payload["gh1_local_orderbook"]["read_only"] is True


def test_adapter_wall_clock_timeout_closes_quiet_stream(tmp_path: Path) -> None:
    adapter = ReadOnlyOrderbookWebSocketAdapter(
        tickers=[TICKER],
        auth_headers=AUTH,
        staging_dir=tmp_path,
        rest_client=_FakeRestClient(),  # type: ignore[arg-type]
        connector=lambda *_args, **_kwargs: _FakeContext(_QuietConnection()),
    )

    summary = asyncio.run(adapter.run(max_messages=10, max_seconds=0.01))

    assert summary.timed_out is True
    assert summary.messages_seen == 0
    assert summary.staged_files == ()


class _FakeRestClient:
    def get_market(self, ticker: str) -> dict[str, str]:
        return {"ticker": ticker, "series_ticker": "KXTEMPNYCH", "status": "open"}

    def get_orderbook(self, _ticker: str) -> dict[str, object]:
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.45", "12"]],
                "no_dollars": [["0.54", "9"]],
            }
        }


class _FakeConnection:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self):
        async def _messages():
            for message in self.messages:
                yield message

        return _messages()


class _QuietConnection(_FakeConnection):
    def __init__(self) -> None:
        super().__init__([])

    def __aiter__(self):
        async def _messages():
            await asyncio.sleep(60)
            if False:
                yield ""

        return _messages()


class _FakeContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


def _snapshot(*, seq: int) -> dict[str, object]:
    return {
        "type": "orderbook_snapshot",
        "sid": 7,
        "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "yes_dollars_fp": [["0.42", "10"]],
            "no_dollars_fp": [["0.53", "10"]],
        },
    }


def _delta(*, seq: int, side: str, price: str, delta: str) -> dict[str, object]:
    return {
        "type": "orderbook_delta",
        "sid": 7,
        "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "side": side,
            "price_dollars": price,
            "delta_fp": delta,
        },
    }
