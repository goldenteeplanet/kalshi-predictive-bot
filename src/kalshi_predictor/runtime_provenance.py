"""PROV-2 read-only runtime forecast/ranking provenance adapter."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import Any

from kalshi_predictor.forecast_provenance import (
    RankingProvenance,
    verify_envelope,
    write_synthetic_provenance_audit,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now


def write_runtime_provenance_audit(
    *, database_path: Path, output_dir: Path, model_names: list[str] | None = None,
    max_rows: int = 100, golden_report: Path | None = None,
) -> Path:
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    golden_path = golden_report or write_synthetic_provenance_audit(
        output_dir / "synthetic_golden"
    )
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    expected_fields = {field.name for field in fields(RankingProvenance)}

    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = _tables(connection)
        required = {"forecasts", "market_rankings", "market_snapshots"}
        if not required.issubset(tables):
            missing = sorted(required - tables)
            raise ValueError(f"runtime provenance tables missing: {','.join(missing)}")
        forecast_rows = _latest_forecasts(connection, model_names, max_rows)
        envelopes, diagnostics = _adapt_rows(connection, tables, forecast_rows)
    finally:
        connection.close()

    golden_fields = {
        key for row in golden.get("records", []) for key in row.get("attribution", {})
    }
    runtime_fields = {
        key for row in envelopes for key in row.get("attribution", {})
    }
    integrity_valid = all(verify_envelope(row) for row in envelopes)
    completeness = sum(not row["diagnostics"] for row in envelopes)
    report = {
        "phase": "PROV-2", "generated_at": utc_now().isoformat(),
        "mode": "RUNTIME_SQLITE_READ_ONLY_PROVENANCE_ADAPTER",
        "database_path": str(database_path), "database_open_mode": "mode=ro+query_only",
        "database_writes": 0, "execution_enabled": False,
        "synthetic_golden_report": str(golden_path),
        "rows": envelopes,
        "summary": {
            "runtime_rows": len(envelopes), "complete_rows": completeness,
            "incomplete_rows": len(envelopes) - completeness,
            "all_runtime_digests_valid": integrity_valid,
            "golden_chain_valid": bool(golden.get("summary", {}).get("chain_valid")),
            "golden_contract_match": (
                expected_fields == golden_fields and expected_fields == runtime_fields
                if envelopes else expected_fields == golden_fields
            ),
            "diagnostic_counts": _counts(diagnostics),
            "activation_or_execution_changed": False,
        },
    }
    path = output_dir / "prov2_runtime_forecast_ranking_provenance.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _adapt_rows(connection: sqlite3.Connection, tables: set[str], forecasts: list[sqlite3.Row]
                ) -> tuple[list[dict[str, Any]], list[str]]:
    envelopes: list[dict[str, Any]] = []
    all_diagnostics: list[str] = []
    previous = "GENESIS"
    for forecast in forecasts:
        ticker, model = str(forecast["ticker"]), str(forecast["model_name"])
        ranking = connection.execute(
            "SELECT * FROM market_rankings WHERE ticker=? AND forecast_model=? "
            "ORDER BY ranked_at DESC, id DESC LIMIT 1", (ticker, model),
        ).fetchone()
        snapshot = connection.execute(
            "SELECT * FROM market_snapshots WHERE ticker=? AND captured_at<=? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1",
            (ticker, ranking["ranked_at"] if ranking else forecast["forecasted_at"]),
        ).fetchone()
        feature = None
        if "features" in tables:
            feature = connection.execute(
                "SELECT * FROM features WHERE ticker=? AND generated_at<=? "
                "ORDER BY generated_at DESC, id DESC LIMIT 1",
                (ticker, forecast["forecasted_at"]),
            ).fetchone()
        feature_payload = _json_object(forecast["feature_json"])
        diagnostics: list[str] = []
        if ranking is None:
            diagnostics.append("RANKING_MISSING")
        if snapshot is None:
            diagnostics.append("ORDERBOOK_SNAPSHOT_MISSING")
        if feature is None:
            diagnostics.append("PERSISTED_FEATURE_ROW_MISSING")
        model_version = _first(feature_payload, "model_version", "version")
        if model_version is None:
            diagnostics.append("MODEL_VERSION_MISSING")
        observation_id = _first(feature_payload, "observation_id", "source_id")
        observation_at = _first(
            feature_payload, "observation_timestamp", "observed_at", "source_timestamp"
        )
        if observation_id is None or observation_at is None:
            diagnostics.append("OBSERVATION_ATTRIBUTION_MISSING")
        record = RankingProvenance(
            ticker=ticker,
            forecast_id=f"forecast:{forecast['id']}",
            forecast_generated_at=str(forecast["forecasted_at"]),
            observation_id=str(observation_id or "MISSING"),
            observation_timestamp=str(observation_at or "MISSING"),
            feature_set_id=(
                f"feature:{feature['id']}:{feature['feature_set_name']}"
                if feature else f"forecast-feature-json:{forecast['id']}"
            ),
            feature_generated_at=str(feature["generated_at"] if feature else forecast["forecasted_at"]),
            model_name=model, model_version=str(model_version or "MISSING"),
            orderbook_snapshot_id=(f"snapshot:{snapshot['id']}" if snapshot else "MISSING"),
            orderbook_timestamp=str(snapshot["captured_at"] if snapshot else "MISSING"),
            ranking_generated_at=str(ranking["ranked_at"] if ranking else "MISSING"),
            previous_digest=previous,
        )
        envelope = record.envelope()
        envelope["diagnostics"] = sorted(set(diagnostics))
        envelope["runtime_values"] = {
            "yes_probability": forecast["yes_probability"],
            "opportunity_score": ranking["opportunity_score"] if ranking else None,
        }
        envelopes.append(envelope)
        previous = envelope["digest"]
        all_diagnostics.extend(envelope["diagnostics"])
    return envelopes, all_diagnostics


def _latest_forecasts(connection: sqlite3.Connection, models: list[str] | None,
                      limit: int) -> list[sqlite3.Row]:
    selected: dict[tuple[str, str], sqlite3.Row] = {}
    if models:
        per_model_limit = max(limit, (limit // len(models)) * 4)
        for model in models:
            rows = connection.execute(
                "SELECT * FROM forecasts WHERE model_name=? "
                "ORDER BY forecasted_at DESC, id DESC LIMIT ?",
                (model, per_model_limit),
            ).fetchall()
            for row in rows:
                selected.setdefault((str(row["ticker"]), str(row["model_name"])), row)
    else:
        rows = connection.execute(
            "SELECT * FROM forecasts ORDER BY forecasted_at DESC, id DESC LIMIT ?",
            (limit * 4,),
        ).fetchall()
        for row in rows:
            selected.setdefault((str(row["ticker"]), str(row["model_name"])), row)
    return sorted(
        selected.values(),
        key=lambda row: (str(row["forecasted_at"]), str(row["ticker"])),
        reverse=True,
    )[:limit]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _first(value, *keys)
            if found not in (None, ""):
                return found
    return None


def _counts(values: list[str]) -> dict[str, int]:
    return {value: values.count(value) for value in sorted(set(values))}
