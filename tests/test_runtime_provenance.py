import hashlib
import json
import sqlite3
from pathlib import Path

from kalshi_predictor.runtime_provenance import write_runtime_provenance_audit


def _database(path: Path, *, complete: bool = True) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE forecasts(
            id INTEGER PRIMARY KEY, ticker TEXT, forecasted_at TEXT, model_name TEXT,
            yes_probability TEXT, feature_json TEXT
        );
        CREATE TABLE market_rankings(
            id INTEGER PRIMARY KEY, ticker TEXT, ranked_at TEXT, forecast_model TEXT,
            opportunity_score TEXT
        );
        CREATE TABLE market_snapshots(
            id INTEGER PRIMARY KEY, ticker TEXT, captured_at TEXT
        );
        CREATE TABLE features(
            id INTEGER PRIMARY KEY, ticker TEXT, feature_set_name TEXT, generated_at TEXT
        );
    """)
    feature = json.dumps({
        "model_version": "2.1.0", "observation_id": "obs:1",
        "observation_timestamp": "2026-07-16T16:00:00+00:00",
    }) if complete else "{}"
    connection.execute("INSERT INTO forecasts VALUES (1,'TICKER','2026-07-16T16:02:00+00:00',"
                       "'weather_v2','0.60',?)", (feature,))
    connection.execute("INSERT INTO market_rankings VALUES "
                       "(2,'TICKER','2026-07-16T16:04:00+00:00','weather_v2','72')")
    connection.execute("INSERT INTO market_snapshots VALUES "
                       "(3,'TICKER','2026-07-16T16:03:00+00:00')")
    if complete:
        connection.execute("INSERT INTO features VALUES "
                           "(4,'TICKER','weather_exact','2026-07-16T16:01:00+00:00')")
    connection.commit()
    connection.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prov2_exports_complete_runtime_chain_without_writes(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    _database(database)
    before = _sha(database)
    path = write_runtime_provenance_audit(
        database_path=database, output_dir=tmp_path / "report",
        model_names=["weather_v2"],
    )
    report = json.loads(path.read_text())
    assert _sha(database) == before
    assert report["database_open_mode"] == "mode=ro+query_only"
    assert report["summary"]["complete_rows"] == 1
    assert report["summary"]["all_runtime_digests_valid"] is True
    assert report["summary"]["golden_contract_match"] is True
    assert report["rows"][0]["attribution"]["orderbook_timestamp"]


def test_prov2_reports_missing_runtime_attribution_without_fabricating_it(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    _database(database, complete=False)
    report = json.loads(write_runtime_provenance_audit(
        database_path=database, output_dir=tmp_path / "report"
    ).read_text())
    diagnostics = report["rows"][0]["diagnostics"]
    assert "MODEL_VERSION_MISSING" in diagnostics
    assert "OBSERVATION_ATTRIBUTION_MISSING" in diagnostics
    assert "PERSISTED_FEATURE_ROW_MISSING" in diagnostics
    assert report["rows"][0]["attribution"]["model_version"] == "MISSING"
    assert report["database_writes"] == 0
