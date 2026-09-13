import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.overnight_paper.current_research_dashboard import current_research_snapshot
from kalshi_predictor.overnight_paper.current_research_store import append_current_record
from kalshi_predictor.overnight_paper.dashboard import create_router, snapshot

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def database(path):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE overnight_shadow(id,event_ticker,payload,evaluation_json,paper_order_id);"
        "CREATE TABLE overnight_history(event_ticker);"
        "CREATE TABLE overnight_sprint_cycles(id TEXT PRIMARY KEY,captured_at TEXT,payload TEXT);"
        "CREATE TABLE paper_orders(id,ticker,side,quantity,status,limit_price,model_name);"
    )
    return db


def scan(at, count):
    return {
        "version": "PAGINATED_CURRENT_RESEARCH_V2", "assessed_at": at.isoformat(),
        "rows": [], "protocol_sha256": "a" * 64,
        "paper_eligible": False, "execution_authority": False,
        "scope": "BOUNDED_DISCOVERY_CURRENT_RESEARCH_NOT_PAPER_ADMISSION",
        "funnel": {"markets_scanned": count, "uncertainty_known": 0, "full_net_gt_5c": 0},
        "first_blocker_counts": {"CALIBRATED_UNCERTAINTY_UNKNOWN": count},
    }


def add_scan(db, at, count, identity):
    append_current_record(
        db, kind="SCAN", identity=identity, payload=scan(at, count), recorded_at=at)


def test_latest_funnel_is_distinct_from_complete_history_and_stales(tmp_path):
    with database(tmp_path / "p.db") as db:
        db.execute("BEGIN")
        add_scan(db, NOW, 10, "first")
        add_scan(db, NOW+timedelta(seconds=20), 3, "last")
        result = current_research_snapshot(db, now=NOW+timedelta(seconds=30))
        assert result["scan_count"] == 2
        assert result["journal_records"] == 2
        assert result["latest_scan_funnel"]["markets_scanned"] == 3
        assert result["latest_scan_freshness"] == "RECENT_RECORDED_SCAN"
        assert result["runtime_verified"] is False
        assert result["paper_eligible"] is False
        assert result["full_net_point_estimate_status"] == "UNKNOWN"
        assert current_research_snapshot(db, now=NOW+timedelta(hours=1))[
            "latest_scan_freshness"] == "STALE"


def test_invalid_current_journal_fails_closed_in_entire_dashboard(tmp_path):
    path = tmp_path / "p.db"
    with database(path) as db:
        db.execute("BEGIN")
        add_scan(db, NOW, 10, "first")
        raw = db.execute("SELECT payload FROM overnight_sprint_cycles").fetchone()[0]
        value = json.loads(raw)
        value["record"]["funnel"]["markets_scanned"] = 999
        db.execute("UPDATE overnight_sprint_cycles SET payload=?", (json.dumps(value),))
    result = snapshot(path)
    assert result["paper_mode"] == "UNVERIFIED"
    assert result["current_research"]["latest_scan_funnel"] is None
    assert result["current_research"]["assessment_count"] is None


def test_future_scan_and_overflow_do_not_return_partial_counts(tmp_path):
    with database(tmp_path / "p.db") as db:
        db.execute("BEGIN")
        add_scan(db, NOW, 1, "first")
        with pytest.raises(ValueError, match="FUTURE_CLOCK"):
            current_research_snapshot(db, now=NOW-timedelta(seconds=1))
        # Overflow is checked before parsing; bad rows must not become a truncated total.
        db.executemany("INSERT INTO overnight_sprint_cycles VALUES(?,?,?)", [
            (f"current-research-v1:overflow:{i}", NOW.isoformat(), "{}") for i in range(10000)
        ])
        with pytest.raises(ValueError):
            current_research_snapshot(db, now=NOW)


def test_api_exposes_separate_research_counts_without_paper_activation(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    with database(path) as db:
        db.execute("BEGIN")
        add_scan(db, NOW, 4, "first")
    monkeypatch.setenv("OVERNIGHT_PAPER_DB", str(path))
    app = FastAPI()
    app.include_router(create_router())
    with TestClient(app) as client:
        value = client.get("/api/paper-live").json()
        assert value["current_research"]["scan_count"] == 1
        assert value["research_assessment_count"] == 0
        assert value["shadow_candidates"] == 0
        assert value["open_positions"] == 0
        assert value["runtime_state"] == "UNVERIFIED"
        assert value["paper_mode"] == "NOT_ACTIVE"
        assert "Full net EV remains unknown" in client.get("/paper-live").text
