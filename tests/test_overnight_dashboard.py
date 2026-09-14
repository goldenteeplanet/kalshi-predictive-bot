import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.overnight_paper.dashboard import create_router, render, snapshot


def test_dashboard_missing_evidence_never_claims_active(tmp_path, monkeypatch):
    path = tmp_path / "missing.db"
    monkeypatch.setenv("OVERNIGHT_PAPER_DB", str(path))
    app = FastAPI()
    app.include_router(create_router())
    with TestClient(app) as client:
        payload = client.get("/api/paper-live").json()
        assert payload["paper_mode"] == "NOT_ACTIVE"
        assert payload["live_exchange"] == payload["demo_exchange"] == "DISABLED"
        assert client.post("/paper-live").status_code == 405
        assert "LOCAL PAPER — NO REAL MONEY" in client.get("/paper-live").text
    assert not path.exists()


def test_public_values_are_escaped():
    payload = snapshot(None)
    payload["blockers"] = ["<script>alert(1)</script>"]
    page = render(payload)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_unverified_pnl_cannot_be_reported_as_a_settled_position(tmp_path):
    path = tmp_path / "paper.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE overnight_shadow(id,event_ticker,payload,evaluation_json,paper_order_id);"
            "CREATE TABLE overnight_history(event_ticker);"
            "CREATE TABLE overnight_sprint_cycles(id,captured_at,payload);"
            "CREATE TABLE paper_orders(id,ticker,side,quantity,status,limit_price,model_name);"
            "CREATE TABLE paper_pnl(id,ticker,realized_pnl,settlement_result);"
            "INSERT INTO paper_orders VALUES(1,'TEST','BUY_YES',1,'FILLED','0.5','model');"
            "INSERT INTO paper_pnl VALUES(1,'TEST','0.5','yes');"
        )
        db.execute(
            "INSERT INTO overnight_shadow VALUES(?,?,?,?,?)",
            ("shadow", "event", json.dumps({"ticker": "TEST"}), None, 1),
        )
    result = snapshot(path)
    assert result["paper_mode"] == "UNVERIFIED"
    assert result["settled"] is None
    assert result["realized_pnl"] is None
    assert result["blockers"] == ["PAPER_DASHBOARD_EVIDENCE_INVALID"]
