import json
import sqlite3

import pytest

from kalshi_predictor.overnight_paper.store import (
    initialize_store,
    record_historical,
    record_shadow,
    record_shadow_evaluation,
    score_final,
    verify_shadow,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "paper.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE overnight_cycles(id INTEGER);"
        "CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,ticker TEXT);"
        "CREATE TABLE paper_fills(id INTEGER);CREATE TABLE paper_positions(id INTEGER);"
    )
    db.close()
    initialize_store(path)
    db = sqlite3.connect(path)
    yield db
    db.close()


@pytest.fixture
def decision():
    return {
        "ticker": "TEST",
        "event_ticker": "EVENT",
        "series_ticker": "SERIES",
        "model": "fixture",
        "model_version": "1",
        "forecast": "0.7",
        "snapshot": {"id": 1},
        "price": "0.5",
        "net_ev": "0.1",
        "sizing": {"quantity": 1},
        "risk": {"action": "ALLOW"},
        "source_provenance": {"hash": "fixture"},
        "settlement_rule_version": "fixture",
        "decision_at": "2026-09-08T02:00:00Z",
        "forecast_at": "2026-09-08T01:59:00Z",
        "source_updated_at": "2026-09-08T01:58:00Z",
        "snapshot_at": "2026-09-08T01:59:59Z",
        "close_time": "2026-09-08T03:00:00Z",
        "side": "BUY_YES",
    }


@pytest.fixture
def final():
    return {
        "ticker": "TEST",
        "status": "settled",
        "result": "yes",
        "settled_at": "2026-09-08T03:20:00Z",
        "source_sha256": "fixture",
    }


def test_shadow_is_idempotent_and_has_no_paper_writes(database, decision):
    key = record_shadow(database, decision)
    assert record_shadow(database, decision) == key
    assert database.execute("SELECT count(*) FROM overnight_shadow").fetchone()[0] == 1
    for table in ("paper_orders", "paper_fills", "paper_positions"):
        assert database.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    verify_shadow(database, key, decision)
    with pytest.raises(ValueError, match="SNAPSHOT_MISMATCH"):
        verify_shadow(database, key, {**decision, "price": "0.4"})


def test_shadow_finality_and_metrics(database, decision, final):
    key = record_shadow(database, decision)
    with pytest.raises(ValueError, match="FINAL_RESULT_REQUIRED"):
        record_shadow_evaluation(database, key, {**final, "status": "closed"})
    record_shadow_evaluation(database, key, final)
    evaluated = json.loads(
        database.execute("SELECT evaluation_json FROM overnight_shadow").fetchone()[0]
    )
    assert evaluated["brier"] == "0.09"
    assert float(evaluated["log_loss"]) == pytest.approx(0.35667494)
    record_shadow_evaluation(database, key, final)
    with pytest.raises(ValueError, match="CORRECTION"):
        record_shadow_evaluation(database, key, {**final, "result": "no"})


def test_historical_is_separate_and_rejects_leakage(database, decision, final):
    with pytest.raises(ValueError, match="MISSING_POINT_IN_TIME_PROVENANCE"):
        record_historical(database, decision, final)
    decision = {
        **decision,
        "source_available_at": "2026-09-08T01:58:01Z",
        "snapshot_available_at": "2026-09-08T01:59:59Z",
        "model_training_cutoff": "2026-09-07T00:00:00Z",
        "source_sha256": "a" * 64,
        "snapshot_sha256": "b" * 64,
        "forecast_sha256": "c" * 64,
    }
    record_historical(database, decision, final)
    assert database.execute("SELECT count(*) FROM overnight_shadow").fetchone()[0] == 0
    with pytest.raises(ValueError, match="LEAKAGE"):
        record_historical(database, {**decision, "forecast_at": "2026-09-08T03:30:00Z"}, final)
    with pytest.raises(ValueError, match="IDENTITY"):
        score_final(decision, {**final, "ticker": "SIBLING"})
    with pytest.raises(ValueError, match="FUTURE_INPUT_LEAKAGE"):
        record_historical(
            database, {**decision, "source_available_at": "2026-09-09T00:00:00Z"}, final
        )


def test_nonempty_unowned_database_is_refused(tmp_path):
    path = tmp_path / "existing.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE paper_orders(id);INSERT INTO paper_orders VALUES(1);"
        "CREATE TABLE paper_fills(id);CREATE TABLE paper_positions(id);"
    )
    db.close()
    with pytest.raises(ValueError, match="NONEMPTY_UNOWNED"):
        initialize_store(path)
