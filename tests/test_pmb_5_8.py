import csv
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_predictor.benchmarking.adapter import compare_database_model_versions
from kalshi_predictor.benchmarking.exchange import LimitOrder, ReplayExchange
from kalshi_predictor.benchmarking.imports import import_user_replay
from kalshi_predictor.benchmarking.replay import load_synthetic_episode, replay_episode
from kalshi_predictor.kalshi.orderbook import (
    LocalOrderbook,
    OrderbookProtocolError,
    OrderbookSequenceGap,
)


def _book() -> LocalOrderbook:
    book = LocalOrderbook("SYN")
    book.apply_snapshot({"seq": 1, "msg": {"market_ticker": "SYN",
        "yes_dollars": [["0.40", "5"]], "no_dollars": [["0.55", "4"]]}})
    return book


def test_pmb5_ioc_post_only_and_gtc_lifecycle() -> None:
    exchange = ReplayExchange()
    book = _book()
    ioc = exchange.submit(
        LimitOrder("ioc", "SYN", "yes", "buy", Decimal("2"), Decimal("0.45"), "IOC"),
        book,
    )
    assert ioc.event == "FILLED"
    post = exchange.submit(
        LimitOrder("post", "SYN", "yes", "buy", Decimal("1"), Decimal("0.45"),
                   "POST_ONLY"), book,
    )
    assert post.reason == "POST_ONLY_WOULD_CROSS"
    gtc = exchange.submit(
        LimitOrder("gtc", "SYN", "yes", "buy", Decimal("3"), Decimal("0.40"), "GTC"),
        book,
    )
    assert gtc.event == "RESTING"
    assert exchange.resting["gtc"].queue_ahead == Decimal("5")


def test_pmb5_queue_partial_fill_cancel_and_replace_are_deterministic() -> None:
    exchange = ReplayExchange()
    book = _book()
    exchange.submit(
        LimitOrder("a", "SYN", "yes", "buy", Decimal("3"), Decimal("0.40"), "GTC"),
        book,
    )
    assert exchange.process_trade(
        ticker="SYN", outcome="yes", price=Decimal("0.40"), size=Decimal("6")
    )[0].filled_size == Decimal("1")
    assert exchange.resting["a"].size == Decimal("2")
    cancelled, replacement = exchange.replace(
        "a", LimitOrder("b", "SYN", "yes", "buy", Decimal("2"), Decimal("0.39"),
                        "GTC"), book,
    )
    assert cancelled.event == "CANCELLED"
    assert replacement.event == "RESTING"
    assert exchange.cancel("b").event == "CANCELLED"


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "ticker", "kind", "message_json"])
        writer.writeheader()
        writer.writerows(rows)


def test_pmb6_json_and_csv_user_owned_imports(tmp_path: Path) -> None:
    payload = {"episode_id": "owned", "category": "weather", "settlements": {},
               "events": [{"timestamp": "2026-01-01T00:00:00Z", "ticker": "SYN",
                            "kind": "snapshot", "message": {"seq": 1, "msg": {
                                "market_ticker": "SYN", "yes_dollars": [],
                                "no_dollars": []}}}]}
    json_path = tmp_path / "owned.json"
    json_path.write_text(json.dumps(payload))
    assert import_user_replay(json_path).episode is not None
    csv_path = tmp_path / "owned.csv"
    _write_csv(csv_path, [{"timestamp": payload["events"][0]["timestamp"],
                           "ticker": "SYN", "kind": "snapshot",
                           "message_json": json.dumps(payload["events"][0]["message"])}])
    result = import_user_replay(csv_path)
    assert result.episode is not None
    assert result.user_owned_data_only is True


def test_pmb6_schema_sequence_and_timestamp_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"events": [
        {"timestamp": "2026-01-01T00:00:02Z", "ticker": "SYN", "kind": "delta",
         "message": {"seq": 2, "msg": {}}},
        {"timestamp": "2026-01-01T00:00:01Z", "ticker": "SYN", "kind": "delta",
         "message": {"seq": 2, "msg": {}}},
    ]}))
    result = import_user_replay(path)
    assert result.episode is None
    assert any("DELTA_BEFORE_SNAPSHOT" in row for row in result.diagnostics)
    assert any("DUPLICATE_SEQUENCE" in row for row in result.diagnostics)
    assert any("OUT_OF_ORDER_TIMESTAMP" in row for row in result.diagnostics)


def test_pmb7_read_only_forecast_ranking_version_comparison(tmp_path: Path) -> None:
    database = tmp_path / "models.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE forecasts(ticker TEXT, model_name TEXT, forecasted_at TEXT,
                               yes_probability TEXT);
        CREATE TABLE rankings(ticker TEXT, forecast_model TEXT, opportunity_score TEXT);
        INSERT INTO forecasts VALUES ('A','v1','2026-01-01','0.40');
        INSERT INTO forecasts VALUES ('A','v2','2026-01-02','0.50');
        INSERT INTO forecasts VALUES ('B','v1','2026-01-01','0.60');
        INSERT INTO forecasts VALUES ('B','v2','2026-01-02','0.55');
        INSERT INTO rankings VALUES ('A','v1','80'),('B','v1','70');
        INSERT INTO rankings VALUES ('A','v2','60'),('B','v2','90');
    """)
    connection.commit()
    connection.close()
    result = compare_database_model_versions(database, baseline_model="v1", candidate_model="v2")
    assert result["database_writes"] == 0
    assert result["rank_changed"] == 2
    assert result["rows"][0]["probability_change"] == "0.10"


def test_pmb8_missing_snapshot_duplicate_gap_and_liquidity_fail_safely() -> None:
    book = LocalOrderbook("SYN")
    with pytest.raises(OrderbookProtocolError):
        book.apply_delta({"seq": 1, "msg": {"market_ticker": "SYN", "side": "yes",
                          "price_dollars": "0.4", "delta_fp": "1"}})
    book = _book()
    with pytest.raises(OrderbookSequenceGap):
        book.apply_delta({"seq": 1, "msg": {"market_ticker": "SYN", "side": "yes",
                          "price_dollars": "0.4", "delta_fp": "1"}})
    with pytest.raises(OrderbookSequenceGap):
        book.apply_delta({"seq": 3, "msg": {"market_ticker": "SYN", "side": "yes",
                          "price_dollars": "0.4", "delta_fp": "1"}})
    empty = LocalOrderbook("EMPTY")
    empty.apply_snapshot({"seq": 1, "msg": {"market_ticker": "EMPTY",
                         "yes_dollars": [], "no_dollars": []}})
    assert empty.execution_quote(outcome="yes", action="buy", size="1").filled_size == 0
    thin = _book().execution_quote(outcome="yes", action="buy", size="10")
    assert thin.fully_executable is False


def test_pmb8_rest_snapshot_recovery_is_repeatable() -> None:
    episode = load_synthetic_episode({"events": [{
        "timestamp": "2026-01-01T00:00:00Z", "ticker": "SYN", "kind": "snapshot",
        "message": {"seq": 5, "msg": {"market_ticker": "SYN",
            "yes_dollars": [["0.4", "1"]], "no_dollars": [["0.5", "1"]]}},
    }], "settlements": {}})
    assert replay_episode(episode) == replay_episode(episode)
