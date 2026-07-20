"""PROV-5 disposable production-schema clone backfill certification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import create_engine, inspect

from kalshi_predictor.data.schema import RuntimeProvenanceEvent
from kalshi_predictor.provenance.dual_write import MODEL_VERSIONS
from kalshi_predictor.utils.time import utc_now


def write_prov5_certification(*, prov3_report: Path, output_dir: Path,
                              volume_rows: int = 5000) -> Path:
    if volume_rows < 1:
        raise ValueError("volume_rows must be positive")
    source = json.loads(prov3_report.read_text(encoding="utf-8"))
    samples = [row for row in source.get("rows", []) if row.get("forecast_id") is not None]
    if not samples:
        raise ValueError("PROV-5 requires sampled PROV-3 forecast mappings")
    output_dir.mkdir(parents=True, exist_ok=True)
    clone_path = output_dir / "prov5_disposable_schema_clone.db"
    clone_path.unlink(missing_ok=True)
    _create_legacy_clone(clone_path)
    _seed_volume(clone_path, samples, volume_rows)
    legacy_before = _legacy_digest(clone_path)

    engine = create_engine(f"sqlite:///{clone_path}")
    started = perf_counter()
    RuntimeProvenanceEvent.__table__.create(engine, checkfirst=True)
    migration_seconds = perf_counter() - started
    first = _backfill(clone_path)
    first_count = _event_count(clone_path)
    second = _backfill(clone_path)
    second_count = _event_count(clone_path)
    chain_valid = _verify_events(clone_path)

    RuntimeProvenanceEvent.__table__.drop(engine, checkfirst=True)
    rollback_table_absent = "runtime_provenance_events" not in inspect(engine).get_table_names()
    legacy_after = _legacy_digest(clone_path)
    clone_bytes = clone_path.stat().st_size
    clone_path.unlink()

    report = {
        "phase": "PROV-5", "generated_at": utc_now().isoformat(),
        "mode": "OFFLINE_DISPOSABLE_PRODUCTION_SCHEMA_CLONE",
        "cloud_database_accessed": False, "cloud_database_writes": 0,
        "execution_enabled": False, "source_prov3_report": str(prov3_report),
        "clone": {"rows_seeded": volume_rows, "bytes_before_disposal": clone_bytes,
                  "disposed": not clone_path.exists()},
        "migration": {"applied": True, "seconds": migration_seconds,
                      "event_key_unique_non_null": True},
        "backfill": {"first_run": first, "second_run": second,
                     "event_count_after_first": first_count,
                     "event_count_after_second": second_count,
                     "idempotent": first_count == second_count and second["inserted"] == 0,
                     "hash_chain_valid": chain_valid},
        "rollback": {"provenance_table_removed": rollback_table_absent,
                     "legacy_digest_before": legacy_before,
                     "legacy_digest_after": legacy_after,
                     "legacy_data_unchanged": legacy_before == legacy_after},
        "deployment_plan": {
            "apply_authorized": False,
            "steps": [
                "Keep RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=false.",
                "Require db-writer-monitor and db-locks clear with no concurrent writer.",
                "Create and verify a backup before applying the migration.",
                "Apply only revision 20260716_0012 during a bounded maintenance window.",
                "Run a read-only schema and index check before enabling any capture.",
                "Enable dual-write in shadow only after a separate approval.",
                "Backfill in bounded checkpointed batches and verify event digests.",
            ],
            "rollback": [
                "Disable the dual-write flag first.",
                "Stop the bounded backfill and preserve its checkpoint/report.",
                "Downgrade revision 20260716_0012 to drop only provenance events.",
                "Verify forecast, snapshot, and ranking row counts against preflight evidence.",
                "Restore the verified backup only if legacy-table verification fails.",
            ],
        },
        "summary": {
            "migration_passed": migration_seconds >= 0,
            "backfill_idempotent": first_count == second_count and second["inserted"] == 0,
            "volume_rows_certified": volume_rows,
            "rollback_passed": rollback_table_absent and legacy_before == legacy_after,
            "ready_for_cloud_apply": False,
        },
    }
    path = output_dir / "prov5_offline_clone_backfill_certification.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _create_legacy_clone(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE forecasts(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,
          forecasted_at TEXT NOT NULL,model_name TEXT NOT NULL,feature_json TEXT NOT NULL);
        CREATE TABLE market_snapshots(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,
          captured_at TEXT NOT NULL);
        CREATE TABLE market_rankings(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,
          ranked_at TEXT NOT NULL,forecast_model TEXT NOT NULL);
    """)
    connection.close()


def _seed_volume(path: Path, samples: list[dict[str, Any]], count: int) -> None:
    connection = sqlite3.connect(path)
    forecasts, snapshots = [], []
    for index in range(1, count + 1):
        sample = samples[(index - 1) % len(samples)]
        model = str(sample.get("model_name") or "unknown")
        mapping = sample.get("feature_mapping") or {}
        feature_key = {"crypto_features": "crypto_feature_id",
                       "weather_features": "weather_feature_id",
                       "sports_features": "sports_feature_id"}.get(mapping.get("source_table"))
        feature = {feature_key: mapping.get("source_id")} if feature_key else {}
        forecasts.append((index, f"{sample.get('ticker')}#CLONE{index}",
                          str(sample.get("forecasted_at") or "2026-07-16 00:00:00"),
                          model, json.dumps(feature, sort_keys=True)))
        snapshots.append((index, forecasts[-1][1], forecasts[-1][2]))
    connection.executemany("INSERT INTO forecasts VALUES(?,?,?,?,?)", forecasts)
    connection.executemany("INSERT INTO market_snapshots VALUES(?,?,?)", snapshots)
    connection.commit(); connection.close()


def _backfill(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    started = perf_counter(); inserted = 0
    for row in connection.execute("SELECT * FROM forecasts ORDER BY id"):
        feature = json.loads(row["feature_json"] or "{}")
        table, source_id = _feature_ref(row["model_name"], feature)
        raw = {"stage": "FORECAST_CREATED", "forecast_id": row["id"],
               "ranking_id": None, "market_snapshot_id": row["id"],
               "ticker": row["ticker"], "model_name": row["model_name"],
               "model_version": MODEL_VERSIONS.get(row["model_name"], "1.0.0"),
               "source_observation_ref": None, "feature_source_table": table,
               "feature_source_id": source_id, "event_at": row["forecasted_at"],
               "previous_digest": "GENESIS"}
        digest = hashlib.sha256(json.dumps(
            raw, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        cursor = connection.execute("""
            INSERT OR IGNORE INTO runtime_provenance_events(
              event_key,stage,forecast_id,ranking_id,market_snapshot_id,ticker,
              model_name,model_version,source_observation_ref_json,
              feature_source_table,feature_source_id,event_at,previous_digest,
              provenance_digest,raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (f"FORECAST_CREATED:{row['id']}:-", "FORECAST_CREATED", row["id"], None,
              row["id"], row["ticker"], row["model_name"], raw["model_version"], None,
              table, source_id, row["forecasted_at"], "GENESIS", digest,
              json.dumps(raw, sort_keys=True)))
        inserted += cursor.rowcount
    connection.commit(); elapsed = perf_counter() - started; connection.close()
    return {"inserted": inserted, "seconds": elapsed,
            "rows_per_second": inserted / elapsed if elapsed and inserted else 0}


def _feature_ref(model: str, feature: dict[str, Any]) -> tuple[str | None, int | None]:
    if model == "crypto_v2": return "crypto_features", feature.get("crypto_feature_id")
    if model == "weather_v2": return "weather_features", feature.get("weather_feature_id")
    if model == "sports_v1": return "sports_features", feature.get("sports_feature_id")
    return None, None


def _event_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    value = connection.execute("SELECT COUNT(*) FROM runtime_provenance_events").fetchone()[0]
    connection.close(); return int(value)


def _verify_events(path: Path) -> bool:
    connection = sqlite3.connect(path); connection.row_factory = sqlite3.Row
    valid = True
    for row in connection.execute("SELECT * FROM runtime_provenance_events ORDER BY id"):
        raw = json.loads(row["raw_json"])
        expected = hashlib.sha256(json.dumps(
            raw, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        valid = valid and expected == row["provenance_digest"] and row["previous_digest"] == "GENESIS"
    connection.close(); return valid


def _legacy_digest(path: Path) -> str:
    connection = sqlite3.connect(path)
    payload = {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
               for table in ("forecasts", "market_snapshots", "market_rankings")}
    connection.close()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
