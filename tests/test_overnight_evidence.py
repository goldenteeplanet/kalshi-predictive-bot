import hashlib
import json
import sqlite3

import pytest

from kalshi_predictor.overnight_paper.evidence import import_weather_archive


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
