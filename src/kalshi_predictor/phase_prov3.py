"""PROV-3 exact runtime attribution mapping and no-write schema preview."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


MODEL_SOURCE_RULES = {
    "crypto_v2": {"table": "crypto_features", "id_key": "crypto_feature_id",
                  "time_column": "generated_at"},
    "sports_v1": {"table": "sports_features", "id_key": "sports_feature_id",
                  "time_column": "created_at"},
    "weather_v2": {"table": "weather_features", "id_key": None,
                   "time_column": "generated_at"},
}


def write_prov3_preview(*, database_path: Path, prov2_report: Path,
                        output_dir: Path, max_rows: int = 100) -> Path:
    prov2 = json.loads(prov2_report.read_text(encoding="utf-8"))
    source_rows = prov2.get("rows", [])[:max_rows]
    before = database_path.stat().st_size
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    tables = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    rows = []
    try:
        for source in source_rows:
            attribution = source.get("attribution", {})
            forecast_id = _integer_suffix(attribution.get("forecast_id"))
            forecast = connection.execute(
                "SELECT * FROM forecasts WHERE id=?", (forecast_id,)
            ).fetchone()
            if forecast is None:
                rows.append({"forecast_id": forecast_id, "mapping_passed": False,
                             "blockers": ["FORECAST_ROW_MISSING"]})
                continue
            feature_json = _json_object(forecast["feature_json"])
            model = str(forecast["model_name"])
            feature_ref = _resolve_feature(connection, tables, model, feature_json, forecast)
            ranking = connection.execute(
                "SELECT id,ranked_at FROM market_rankings WHERE ticker=? AND forecast_model=? "
                "ORDER BY ranked_at DESC,id DESC LIMIT 1",
                (forecast["ticker"], model),
            ).fetchone()
            snapshot = connection.execute(
                "SELECT id,captured_at FROM market_snapshots WHERE ticker=? "
                "AND captured_at<=? ORDER BY captured_at DESC,id DESC LIMIT 1",
                (forecast["ticker"], forecast["forecasted_at"]),
            ).fetchone()
            blockers = []
            if not feature_ref.get("resolved"):
                blockers.append("MODEL_FEATURE_RELATION_UNRESOLVED")
            if not feature_ref.get("observation_reference_persisted"):
                blockers.append("SOURCE_OBSERVATION_ID_NOT_PERSISTED")
            if ranking is None:
                blockers.append("RANKING_NOT_PERSISTED")
            if snapshot is None:
                blockers.append("SNAPSHOT_RELATION_UNRESOLVED")
            blockers.append("MODEL_VERSION_NOT_PERSISTED")
            rows.append({
                "forecast_id": forecast_id, "ticker": forecast["ticker"],
                "model_name": model, "forecasted_at": forecast["forecasted_at"],
                "model_version_source": "NOT_PERSISTED",
                "feature_mapping": feature_ref,
                "snapshot_mapping": ({"snapshot_id": snapshot["id"],
                                      "captured_at": snapshot["captured_at"],
                                      "exact_forecast_time_match": (
                                          snapshot["captured_at"] == forecast["forecasted_at"]
                                      )} if snapshot else None),
                "ranking_mapping": ({"ranking_id": ranking["id"],
                                     "ranked_at": ranking["ranked_at"]}
                                    if ranking else None),
                "mapping_passed": not blockers, "blockers": sorted(set(blockers)),
            })
    finally:
        connection.close()
    after = database_path.stat().st_size
    blocker_counts = _counts(blocker for row in rows for blocker in row.get("blockers", []))
    report = {
        "phase": "PROV-3", "generated_at": utc_now().isoformat(),
        "mode": "EXACT_RUNTIME_ATTRIBUTION_SOURCE_MAPPING_SCHEMA_PREVIEW",
        "database_open_mode": "mode=ro+query_only", "database_writes": 0,
        "database_size_unchanged": before == after, "execution_enabled": False,
        "source_prov2_report": str(prov2_report), "rows": rows,
        "mapping_rules": MODEL_SOURCE_RULES,
        "schema_repair_preview": {
            "apply_permitted": False,
            "proposed_columns": [
                {"table": "forecasts", "column": "model_version", "type": "TEXT NOT NULL"},
                {"table": "forecasts", "column": "source_observation_ref_json", "type": "TEXT"},
                {"table": "forecasts", "column": "feature_source_table", "type": "TEXT"},
                {"table": "forecasts", "column": "feature_source_id", "type": "INTEGER"},
                {"table": "forecasts", "column": "market_snapshot_id", "type": "INTEGER"},
                {"table": "market_rankings", "column": "forecast_id", "type": "INTEGER"},
                {"table": "market_rankings", "column": "market_snapshot_id", "type": "INTEGER"},
                {"table": "market_rankings", "column": "provenance_digest", "type": "TEXT"},
            ],
            "constraints": [
                "market_rankings.forecast_id REFERENCES forecasts(id)",
                "feature source table/id must match the registered model mapping",
                "provenance_digest is immutable after insert",
                "ranking timestamps cannot precede forecast or snapshot timestamps",
            ],
            "required_runtime_changes": [
                "Forecast writers persist registered model version and exact source IDs.",
                "weather_v2 persists the resolved weather_features.id, not only location/target.",
                "Ranking persistence receives forecast_id and snapshot_id directly.",
                "GH-1U immediate evaluations remain reports until normal ranking persistence is invoked.",
            ],
        },
        "summary": {
            "rows_traced": len(rows),
            "feature_relations_resolved": sum(
                bool(row.get("feature_mapping", {}).get("resolved")) for row in rows
            ),
            "snapshot_relations_resolved": sum(row.get("snapshot_mapping") is not None for row in rows),
            "ranking_relations_resolved": sum(row.get("ranking_mapping") is not None for row in rows),
            "complete_rows": sum(bool(row.get("mapping_passed")) for row in rows),
            "blocker_counts": blocker_counts,
            "schema_change_applied": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov3_exact_attribution_schema_repair_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _resolve_feature(connection: sqlite3.Connection, tables: set[str], model: str,
                     payload: dict[str, Any], forecast: sqlite3.Row) -> dict[str, Any]:
    rule = MODEL_SOURCE_RULES.get(model)
    if not rule or rule["table"] not in tables:
        return {"resolved": False, "reason": "MODEL_SOURCE_RULE_OR_TABLE_MISSING"}
    table = rule["table"]
    if rule["id_key"]:
        feature_id = payload.get(rule["id_key"])
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (feature_id,)).fetchone()
    else:
        location = payload.get("location_key") or payload.get("linked_location_key")
        target = payload.get("target_time")
        row = connection.execute(
            "SELECT * FROM weather_features WHERE location_key=? "
            "AND datetime(target_time)=datetime(?) "
            "AND generated_at<=? ORDER BY generated_at DESC,id DESC LIMIT 1",
            (location, target, forecast["forecasted_at"]),
        ).fetchone()
    if row is None:
        return {"resolved": False, "source_table": table,
                "reason": "EXACT_MODEL_FEATURE_ROW_MISSING"}
    columns = set(row.keys())
    raw = _json_object(row["raw_json"]) if "raw_json" in columns else {}
    observation_id = _first(raw, "observation_id", "source_observation_id")
    return {
        "resolved": True, "source_table": table, "source_id": row["id"],
        "source_time": row[rule["time_column"]],
        "resolution": "EMBEDDED_FEATURE_ID" if rule["id_key"] else "EXACT_LOCATION_TARGET_TIME",
        "observation_reference_persisted": observation_id is not None,
        "observation_id": observation_id,
    }


def _integer_suffix(value: Any) -> int:
    return int(str(value).rsplit(":", 1)[-1])


def _json_object(value: Any) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    for value in payload.values():
        if isinstance(value, dict):
            found = _first(value, *keys)
            if found not in (None, ""):
                return found
    return None


def _counts(values) -> dict[str, int]:
    materialized = list(values)
    return {value: materialized.count(value) for value in sorted(set(materialized))}
