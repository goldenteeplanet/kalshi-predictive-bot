from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.benchmarking.offline_export_join import join_exact_runtime_exports
from kalshi_predictor.benchmarking.shadow_adapter import ExposureGuardShadowAdapter


MAX_ROWS_PER_DATASET = 1000
DATASET_SCHEMAS = {
    "decisions": (
        "ticker", "category", "target_time", "decision_time", "candidate_forecast_id",
        "reference_forecast_id", "current_market_snapshot_id", "reference_market_snapshot_id",
    ),
    "rankings": (
        "ticker", "category", "opportunity_score", "forecast_model", "forecast_id",
        "feature_ref", "observation_ref", "market_snapshot_id", "model_version",
    ),
    "risks": ("ticker", "risk_gate_passed", "requested_capital"),
    "forecasts": (
        "ticker", "category", "target_time", "generated_at", "forecast_id",
        "model_version", "probability",
    ),
    "books": (
        "ticker", "category", "target_time", "captured_at", "market_snapshot_id",
        "executable_spread",
    ),
}
INTEGER_FIELDS = {
    "candidate_forecast_id", "reference_forecast_id", "current_market_snapshot_id",
    "reference_market_snapshot_id", "forecast_id", "market_snapshot_id",
}
BOOLEAN_FIELDS = {"risk_gate_passed"}


def _coerce_csv_value(field: str, value: str) -> Any:
    if field in INTEGER_FIELDS:
        return int(value)
    if field in BOOLEAN_FIELDS:
        lowered = value.strip().lower()
        if lowered not in {"true", "false"}:
            raise ValueError(f"invalid boolean {value!r}")
        return lowered == "true"
    return value


def _read_dataset(path: Path, file_format: str) -> list[dict[str, Any]]:
    if file_format == "json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON dataset must be an array of objects")
        return payload
    if file_format == "csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [
                {field: _coerce_csv_value(field, value) for field, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    raise ValueError(f"unsupported format {file_format!r}")


def import_runtime_export_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    diagnostics: list[str] = []
    imported: dict[str, list[dict[str, Any]]] = {}
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict):
        return {"valid": False, "diagnostics": ["MANIFEST_DATASETS_MISSING"], "datasets": {}}
    unknown = sorted(set(datasets) - set(DATASET_SCHEMAS))
    diagnostics.extend(f"UNKNOWN_DATASET:{name}" for name in unknown)
    for name, required_fields in DATASET_SCHEMAS.items():
        spec = datasets.get(name)
        if not isinstance(spec, dict):
            diagnostics.append(f"DATASET_SPEC_MISSING:{name}")
            continue
        relative = spec.get("path")
        file_format = spec.get("format")
        if relative in (None, "") or file_format not in {"json", "csv"}:
            diagnostics.append(f"DATASET_SPEC_INVALID:{name}")
            continue
        path = (manifest_path.parent / relative).resolve()
        try:
            path.relative_to(manifest_path.parent.resolve())
        except ValueError:
            diagnostics.append(f"DATASET_PATH_OUTSIDE_MANIFEST_ROOT:{name}")
            continue
        if not path.is_file():
            diagnostics.append(f"DATASET_FILE_MISSING:{name}")
            continue
        try:
            rows = _read_dataset(path, file_format)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(f"DATASET_READ_ERROR:{name}:{type(exc).__name__}")
            continue
        if len(rows) > MAX_ROWS_PER_DATASET:
            diagnostics.append(f"DATASET_ROW_LIMIT_EXCEEDED:{name}:{len(rows)}>{MAX_ROWS_PER_DATASET}")
        for index, row in enumerate(rows):
            for field in required_fields:
                if row.get(field) in (None, ""):
                    diagnostics.append(f"SCHEMA_FIELD_MISSING:{name}:{index}:{field}")
        imported[name] = rows
    return {"valid": not diagnostics, "diagnostics": diagnostics, "datasets": imported}


def _exact_associated(
    rows: list[Mapping[str, Any]], decision: Mapping[str, Any], name: str,
    diagnostics: list[str],
) -> Mapping[str, Any] | None:
    matches = [
        row for row in rows
        if row.get("ticker") == decision.get("ticker")
        and (name == "risks" or row.get("category") == decision.get("category"))
    ]
    if len(matches) != 1:
        diagnostics.append(f"ASSOCIATION_{'MISSING' if not matches else 'AMBIGUOUS'}:{name}:{decision.get('ticker')}")
        return None
    return matches[0]


def build_runtime_export_import_preview(manifest_path: Path) -> dict[str, Any]:
    imported = import_runtime_export_manifest(manifest_path)
    rows = []
    adapter = ExposureGuardShadowAdapter()
    if imported["valid"]:
        data = imported["datasets"]
        for index, decision in enumerate(data["decisions"]):
            diagnostics: list[str] = []
            ranking = _exact_associated(data["rankings"], decision, "rankings", diagnostics)
            risk = _exact_associated(data["risks"], decision, "risks", diagnostics)
            join = None
            if not diagnostics:
                join = join_exact_runtime_exports({
                    "decision": decision,
                    "ranking": ranking,
                    "risk": risk,
                    "forecasts": data["forecasts"],
                    "books": data["books"],
                })
                diagnostics.extend(join["diagnostics"])
            certified = not diagnostics and join is not None and join["joined"]
            rows.append({
                "decision_index": index,
                "ticker": decision.get("ticker"),
                "category": decision.get("category"),
                "certified": certified,
                "diagnostics": diagnostics,
                "shadow_preview": adapter.preview(join["normalized"]) if certified else None,
            })
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    category_coverage = sorted({row["category"] for row in rows if row["certified"]})
    return {
        "phase": "PMB-34C",
        "mode": "LOCAL_USER_OWNED_RUNTIME_EXPORT_IMPORT_CERTIFICATION_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "bounded_row_limit_per_dataset": MAX_ROWS_PER_DATASET,
        "supported_formats": ["csv", "json"],
        "manifest_valid": imported["valid"],
        "manifest_diagnostics": imported["diagnostics"],
        "dataset_row_counts": {
            name: len(values) for name, values in sorted(imported["datasets"].items())
        },
        "rows": rows,
        "summary": {
            "decisions": len(rows),
            "certified": sum(row["certified"] for row in rows),
            "rejected": sum(not row["certified"] for row in rows),
            "category_coverage": category_coverage,
            "pmb35_deployment_unblocked": False,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_runtime_export_import_preview(manifest_path: Path, output_dir: Path) -> Path:
    report = build_runtime_export_import_preview(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34c_runtime_export_import_certification_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
