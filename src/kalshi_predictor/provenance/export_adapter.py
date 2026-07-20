from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.provenance.regression import build_attribution_regression_report

SCHEMA_PROV2_ENVELOPE_V1 = "prov2-envelope-v1"
SCHEMA_RUNTIME_EVENT_V2 = "runtime-event-v2"
SCHEMA_NORMALIZED_V3 = "normalized-attribution-v3"
SUPPORTED_SCHEMAS = {
    "1": SCHEMA_PROV2_ENVELOPE_V1,
    "2": SCHEMA_RUNTIME_EVENT_V2,
    "3": SCHEMA_NORMALIZED_V3,
    SCHEMA_PROV2_ENVELOPE_V1: SCHEMA_PROV2_ENVELOPE_V1,
    SCHEMA_RUNTIME_EVENT_V2: SCHEMA_RUNTIME_EVENT_V2,
    SCHEMA_NORMALIZED_V3: SCHEMA_NORMALIZED_V3,
}


def load_runtime_provenance_export(path: Path, *, max_bytes: int = 10_000_000) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError("runtime provenance export exceeds max_bytes")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("runtime provenance export must contain a rows list")
    return payload


def adapt_runtime_provenance_export(
    payload: Mapping[str, Any], *, max_rows: int = 200
) -> list[dict[str, Any]]:
    if max_rows < 1 or max_rows > 1000:
        raise ValueError("max_rows must be between 1 and 1000")
    normalized = normalize_runtime_provenance_export(payload, max_rows=max_rows)
    if normalized["diagnostics"]:
        first = normalized["diagnostics"][0]
        raise ValueError(f"{first['code']}: {first['message']}")
    return normalized["rows"]


def normalize_runtime_provenance_export(
    payload: Mapping[str, Any], *, max_rows: int = 200
) -> dict[str, Any]:
    if max_rows < 1 or max_rows > 1000:
        raise ValueError("max_rows must be between 1 and 1000")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("runtime provenance export must contain a rows list")
    if len(rows) > max_rows:
        raise ValueError("runtime provenance export exceeds max_rows")
    declared = payload.get("schema_version") or payload.get("export_schema_version")
    schema = SUPPORTED_SCHEMAS.get(str(declared)) if declared is not None else None
    diagnostics: list[dict[str, Any]] = []
    if declared is not None and schema is None:
        diagnostics.append(_diagnostic(
            "UNSUPPORTED_SCHEMA_VERSION", None,
            f"unsupported schema version: {declared}",
        ))
    detected = [_detect_row_schema(row) for row in rows]
    known = sorted({value for value in detected if value is not None})
    if schema is None and declared is None:
        if len(known) == 1:
            schema = known[0]
        elif len(known) > 1:
            schema = "mixed"
            diagnostics.append(_diagnostic(
                "MIXED_ROW_SCHEMAS", None, f"mixed row schemas: {','.join(known)}"
            ))
        else:
            diagnostics.append(_diagnostic(
                "SCHEMA_UNDETECTABLE", None, "no supported row schema detected"
            ))
    normalized_rows = []
    for index, row in enumerate(rows):
        row_schema = detected[index]
        if row_schema is None:
            diagnostics.append(_diagnostic(
                "MALFORMED_ROW", index, "row is not a recognized attribution object"
            ))
            continue
        if schema not in (None, "mixed") and row_schema != schema:
            diagnostics.append(_diagnostic(
                "ROW_SCHEMA_MISMATCH", index,
                f"declared {schema}, detected {row_schema}",
            ))
            continue
        try:
            normalized_rows.append(_adapt_row(row, index))
        except (TypeError, ValueError) as exc:
            diagnostics.append(_diagnostic("MALFORMED_ROW", index, str(exc)))
    return {
        "source_schema": schema,
        "normalized_schema": SCHEMA_NORMALIZED_V3,
        "source_row_count": len(rows),
        "normalized_row_count": len(normalized_rows),
        "rows": normalized_rows,
        "diagnostics": diagnostics,
        "compatible": not diagnostics and len(normalized_rows) == len(rows),
    }


def compare_runtime_export_to_golden(
    payload: Mapping[str, Any],
    *,
    expected_model_versions: Mapping[str, Iterable[str]],
    generated_at: datetime,
    thresholds: Mapping[str, float] | None = None,
    max_rows: int = 200,
) -> dict[str, Any]:
    compatibility = normalize_runtime_provenance_export(payload, max_rows=max_rows)
    events = compatibility["rows"]
    report = build_attribution_regression_report(
        events,
        expected_model_versions=expected_model_versions,
        generated_at=generated_at,
        thresholds=thresholds,
    )
    report["phase"] = "PROV-15B"
    report["mode"] = "READ_ONLY_RUNTIME_EXPORT_GOLDEN_COMPARISON"
    report["source_phase"] = payload.get("phase")
    report["source_database_writes"] = payload.get("database_writes", 0)
    report["schema_compatibility"] = {
        key: compatibility[key]
        for key in (
            "source_schema", "normalized_schema", "source_row_count",
            "normalized_row_count", "compatible", "diagnostics",
        )
    }
    if not compatibility["compatible"]:
        report["summary"]["passed"] = False
    report["guardrails"] = {
        "source_file_modified": False,
        "database_access": False,
        "runtime_configuration_changed": False,
        "execution_enabled": False,
    }
    return report


def write_runtime_export_comparison(
    payload: Mapping[str, Any],
    *,
    expected_model_versions: Mapping[str, Iterable[str]],
    generated_at: datetime,
    output_path: Path,
    thresholds: Mapping[str, float] | None = None,
    max_rows: int = 200,
) -> Path:
    report = compare_runtime_export_to_golden(
        payload,
        expected_model_versions=expected_model_versions,
        generated_at=generated_at,
        thresholds=thresholds,
        max_rows=max_rows,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def _adapt_row(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"row {index} must be an object")
    attribution = value.get("attribution")
    if isinstance(attribution, Mapping):
        return _adapt_prov2_envelope(value, attribution, index)
    raw = _json_object(value.get("raw_json"))
    observation = _mapping_or_json(
        value.get("source_observation_ref") or value.get("source_observation_ref_json")
    )
    snapshot = _mapping_or_json(value.get("market_snapshot_ref"))
    snapshot_id = value.get("market_snapshot_id")
    if not snapshot and snapshot_id not in (None, ""):
        snapshot = {"table": "market_snapshots", "id": snapshot_id}
        captured_at = value.get("market_snapshot_at") or raw.get("market_snapshot_at")
        if captured_at:
            snapshot["captured_at"] = captured_at
    return {
        "event_key": str(value.get("event_key") or f"runtime-row-{index}"),
        "ticker": str(value.get("ticker") or raw.get("ticker") or ""),
        "model_name": str(value.get("model_name") or raw.get("model_name") or ""),
        "model_version": str(value.get("model_version") or raw.get("model_version") or ""),
        "event_at": value.get("event_at") or raw.get("event_at"),
        "source_observation_ref": observation,
        "market_snapshot_ref": snapshot,
    }


def _detect_row_schema(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if isinstance(value.get("attribution"), Mapping):
        return SCHEMA_PROV2_ENVELOPE_V1
    if all(key in value for key in (
        "event_key", "model_name", "model_version", "event_at",
        "source_observation_ref", "market_snapshot_ref",
    )):
        return SCHEMA_NORMALIZED_V3
    if any(key in value for key in (
        "event_key", "raw_json", "source_observation_ref_json", "market_snapshot_id",
    )):
        return SCHEMA_RUNTIME_EVENT_V2
    return None


def _diagnostic(code: str, row_index: int | None, message: str) -> dict[str, Any]:
    return {"code": code, "row_index": row_index, "message": message}


def _adapt_prov2_envelope(
    row: Mapping[str, Any], attribution: Mapping[str, Any], index: int
) -> dict[str, Any]:
    observation_id = attribution.get("observation_id")
    snapshot_id = attribution.get("orderbook_snapshot_id")
    observation = None if _missing(observation_id) else {
        "table": "runtime_observations",
        "id": _reference_id(observation_id),
        "observed_at": attribution.get("observation_timestamp"),
    }
    snapshot = None if _missing(snapshot_id) else {
        "table": "market_snapshots",
        "id": _reference_id(snapshot_id),
        "captured_at": attribution.get("orderbook_timestamp"),
    }
    return {
        "event_key": str(row.get("digest") or f"prov2-row-{index}"),
        "ticker": str(attribution.get("ticker") or ""),
        "model_name": str(attribution.get("model_name") or ""),
        "model_version": str(attribution.get("model_version") or ""),
        "event_at": (
            attribution.get("ranking_generated_at")
            if not _missing(attribution.get("ranking_generated_at"))
            else attribution.get("forecast_generated_at")
        ),
        "source_observation_ref": observation,
        "market_snapshot_ref": snapshot,
    }


def _mapping_or_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    parsed = _json_object(value)
    return parsed or None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _reference_id(value: Any) -> int | str:
    text = str(value)
    suffix = text.rsplit(":", 1)[-1]
    return int(suffix) if suffix.isdigit() else text


def _missing(value: Any) -> bool:
    return value in (None, "", "MISSING")
