import hashlib
import json
import sqlite3
from pathlib import Path

from kalshi_predictor.phase_prov3 import write_prov3_preview


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prov3_resolves_model_specific_features_without_writes(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE forecasts(id INTEGER PRIMARY KEY,ticker TEXT,forecasted_at TEXT,
          model_name TEXT,feature_json TEXT);
        CREATE TABLE market_rankings(id INTEGER PRIMARY KEY,ticker TEXT,ranked_at TEXT,
          forecast_model TEXT);
        CREATE TABLE market_snapshots(id INTEGER PRIMARY KEY,ticker TEXT,captured_at TEXT);
        CREATE TABLE crypto_features(id INTEGER PRIMARY KEY,generated_at TEXT,raw_json TEXT);
        CREATE TABLE weather_features(id INTEGER PRIMARY KEY,location_key TEXT,source TEXT,
          generated_at TEXT,target_time TEXT,raw_json TEXT);
        CREATE TABLE sports_features(id INTEGER PRIMARY KEY,created_at TEXT,raw_json TEXT);
        INSERT INTO forecasts VALUES(1,'BTC','2026-07-16 16:02:00','crypto_v2',
          '{"crypto_feature_id":10}');
        INSERT INTO forecasts VALUES(2,'NYC','2026-07-16 16:02:00','weather_v2',
          '{"location_key":"new_york","target_time":"2026-07-16 17:00:00"}');
        INSERT INTO crypto_features VALUES(10,'2026-07-16 16:01:00','{}');
        INSERT INTO weather_features VALUES(20,'new_york','nws','2026-07-16 16:01:00',
          '2026-07-16 17:00:00','{}');
        INSERT INTO market_snapshots VALUES(30,'BTC','2026-07-16 16:02:00');
        INSERT INTO market_snapshots VALUES(31,'NYC','2026-07-16 16:02:00');
    """)
    connection.commit()
    connection.close()
    prov2 = tmp_path / "prov2.json"
    prov2.write_text(json.dumps({"rows": [
        {"attribution": {"forecast_id": "forecast:1"}},
        {"attribution": {"forecast_id": "forecast:2"}},
    ]}))
    before = _sha(database)
    report = json.loads(write_prov3_preview(
        database_path=database, prov2_report=prov2, output_dir=tmp_path / "out"
    ).read_text())
    assert _sha(database) == before
    assert report["summary"]["feature_relations_resolved"] == 2
    assert report["summary"]["snapshot_relations_resolved"] == 2
    assert report["schema_repair_preview"]["apply_permitted"] is False
    assert report["summary"]["schema_change_applied"] is False


def test_prov3_does_not_infer_missing_ranking_or_observation_ids(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE forecasts(id INTEGER PRIMARY KEY,ticker TEXT,forecasted_at TEXT,
          model_name TEXT,feature_json TEXT);
        CREATE TABLE market_rankings(id INTEGER PRIMARY KEY,ticker TEXT,ranked_at TEXT,
          forecast_model TEXT);
        CREATE TABLE market_snapshots(id INTEGER PRIMARY KEY,ticker TEXT,captured_at TEXT);
        CREATE TABLE crypto_features(id INTEGER PRIMARY KEY,generated_at TEXT,raw_json TEXT);
        INSERT INTO forecasts VALUES(1,'BTC','2026-07-16 16:02:00','crypto_v2',
          '{"crypto_feature_id":10}');
        INSERT INTO crypto_features VALUES(10,'2026-07-16 16:01:00','{}');
    """)
    connection.commit(); connection.close()
    prov2 = tmp_path / "prov2.json"
    prov2.write_text(json.dumps({"rows": [{"attribution": {"forecast_id": "forecast:1"}}]}))
    row = json.loads(write_prov3_preview(
        database_path=database, prov2_report=prov2, output_dir=tmp_path / "out"
    ).read_text())["rows"][0]
    assert "RANKING_NOT_PERSISTED" in row["blockers"]
    assert "SOURCE_OBSERVATION_ID_NOT_PERSISTED" in row["blockers"]
    assert "MODEL_VERSION_NOT_PERSISTED" in row["blockers"]
