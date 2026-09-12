import hashlib
import json
import sqlite3

import pytest

from kalshi_predictor.overnight_paper.evidence import (
    import_reported_crypto_finals,
    import_weather_archive,
)


def test_weather_capture_is_diagnostic_idempotent_and_hash_checked(tmp_path):
    archive = tmp_path / "capture"
    archive.mkdir()
    raw = b'{"old_forecast":true}'
    (archive / "response-01.json").write_bytes(raw)
    result = {
        "requests": [{"status": 200, "sha256": hashlib.sha256(raw).hexdigest()}],
        "role": "DIAGNOSTIC_ONLY",
        "max_age_seconds": 1800,
        "finished_at": "2026-09-08T02:00:00Z",
        "forecasts": [{"health": {"state": "STALE"}}],
    }
    (archive / "result.json").write_text(json.dumps(result))
    path = tmp_path / "isolated.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE paper_orders(id INTEGER);CREATE TABLE paper_fills(id INTEGER);"
            "CREATE TABLE paper_positions(id INTEGER);CREATE TABLE forecasts(id INTEGER);"
        )
    key = import_weather_archive(path, archive)
    assert import_weather_archive(path, archive) == key
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM overnight_sprint_cycles").fetchone()[0] == 1
        for table in ("paper_orders", "paper_fills", "forecasts"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        payload = json.loads(
            db.execute("SELECT payload FROM overnight_sprint_cycles").fetchone()[0]
        )
        assert payload["trading_input"] is False
        assert payload["result"]["forecasts"][0]["health"]["state"] == "STALE"
    (archive / "response-01.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        import_weather_archive(path, archive)


def test_reported_final_values_remain_separate_from_independent_truth_and_pnl(tmp_path):
    archive = tmp_path / "finals"
    archive.mkdir()
    market = {
        "ticker": "KXBTC15M-TEST",
        "status": "finalized",
        "result": "yes",
        "close_time": "2026-09-08T01:00:00Z",
        "settlement_ts": "2026-09-08T01:01:00Z",
        "strike_type": "greater_or_equal",
        "expiration_value": "101",
        "floor_strike": 100,
    }
    raw = json.dumps({"markets": [market]}).encode()
    (archive / "response-001.json").write_bytes(raw)
    receipts = [
        {
            "method": "GET",
            "status": 200,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "url": "https://external-api.kalshi.com/trade-api/v2/markets",
            "params": {"series_ticker": "KXBTC15M", "status": "settled", "limit": 5},
            "received_at": "2026-09-08T01:02:00Z",
        }
    ]
    (archive / "requests.json").write_text(json.dumps(receipts))
    path = tmp_path / "isolated.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE paper_orders(id);CREATE TABLE paper_fills(id);"
            "CREATE TABLE paper_positions(id);CREATE TABLE paper_pnl(id);"
        )
    assert import_reported_crypto_finals(path, archive) == import_reported_crypto_finals(
        path, archive
    )
    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM overnight_sprint_cycles").fetchone()[0]
        )
        assert payload["independent_source_reproductions"] == 0
        assert payload["examples"][0]["reproduced_reported_result"] == "yes"
        assert db.execute("SELECT count(*) FROM paper_pnl").fetchone()[0] == 0
    market["status"] = "determined"
    raw = json.dumps({"markets": [market]}).encode()
    (archive / "response-001.json").write_bytes(raw)
    receipts[0]["sha256"] = hashlib.sha256(raw).hexdigest()
    (archive / "requests.json").write_text(json.dumps(receipts))
    with pytest.raises(ValueError, match="SUPPORTED_FINAL_METHOD_REQUIRED"):
        import_reported_crypto_finals(path, archive)
