from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.benchmarking.export_custody import certify_export_custody
from kalshi_predictor.benchmarking.runtime_export_import import import_runtime_export_manifest


DATASET_KEYS = {
    "decisions": ("ticker", "category", "target_time"),
    "rankings": ("ticker", "category"),
    "risks": ("ticker",),
    "forecasts": ("forecast_id",),
    "books": ("market_snapshot_id",),
}
PROVENANCE_FIELDS = {
    "decisions": {
        "candidate_forecast_id", "reference_forecast_id",
        "current_market_snapshot_id", "reference_market_snapshot_id",
    },
    "rankings": {
        "forecast_id", "feature_ref", "observation_ref", "market_snapshot_id", "model_version",
    },
    "forecasts": {"forecast_id", "model_version", "generated_at", "target_time"},
    "books": {"market_snapshot_id", "captured_at", "target_time"},
    "risks": set(),
}


def _row_key(dataset: str, row: Mapping[str, Any]) -> str:
    return "|".join(str(row.get(field, "")) for field in DATASET_KEYS[dataset])


def _index(dataset: str, rows: list[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    diagnostics = []
    for row in rows:
        key = _row_key(dataset, row)
        if key in indexed:
            diagnostics.append(f"DUPLICATE_ROW_KEY:{dataset}:{key}")
        indexed[key] = row
    return indexed, diagnostics


def compare_export_datasets(
    baseline: Mapping[str, list[Mapping[str, Any]]],
    candidate: Mapping[str, list[Mapping[str, Any]]],
    declared_changes: set[str] | None = None,
) -> dict[str, Any]:
    declared = declared_changes or set()
    diagnostics: list[str] = []
    changes = []
    used_declarations: set[str] = set()
    for dataset in DATASET_KEYS:
        before, before_diagnostics = _index(dataset, baseline.get(dataset, []))
        after, after_diagnostics = _index(dataset, candidate.get(dataset, []))
        diagnostics.extend(before_diagnostics + after_diagnostics)
        for key in sorted(set(before) | set(after)):
            if key not in before:
                change_id = f"{dataset}:{key}:ROW_ADDED"
                fields = ["ROW_ADDED"]
            elif key not in after:
                change_id = f"{dataset}:{key}:ROW_REMOVED"
                fields = ["ROW_REMOVED"]
            else:
                fields = sorted(
                    field for field in set(before[key]) | set(after[key])
                    if before[key].get(field) != after[key].get(field)
                )
                if not fields:
                    continue
                change_id = None
            for field in fields:
                identifier = change_id or f"{dataset}:{key}:{field}"
                explained = identifier in declared
                if explained:
                    used_declarations.add(identifier)
                provenance_breaking = (
                    field in {"ROW_ADDED", "ROW_REMOVED"}
                    or field in PROVENANCE_FIELDS[dataset]
                )
                schema_change = (
                    field not in {"ROW_ADDED", "ROW_REMOVED"}
                    and ((field in before.get(key, {})) != (field in after.get(key, {})))
                )
                changes.append({
                    "change_id": identifier,
                    "dataset": dataset,
                    "row_key": key,
                    "field": field,
                    "before": before.get(key, {}).get(field),
                    "after": after.get(key, {}).get(field),
                    "explained": explained,
                    "schema_change": schema_change,
                    "provenance_breaking": provenance_breaking,
                })
                if not explained:
                    diagnostics.append(f"UNEXPLAINED_DRIFT:{identifier}")
                if provenance_breaking and not explained:
                    diagnostics.append(f"PROVENANCE_BREAKING_DRIFT:{identifier}")
    diagnostics.extend(f"DECLARED_CHANGE_NOT_OBSERVED:{item}" for item in sorted(declared - used_declarations))
    canonical = json.dumps(changes, sort_keys=True, separators=(",", ":")).encode()
    return {
        "certified": not diagnostics,
        "diagnostics": diagnostics,
        "changes": changes,
        "summary": {
            "changes": len(changes),
            "explained": sum(change["explained"] for change in changes),
            "unexplained": sum(not change["explained"] for change in changes),
            "schema_changes": sum(change["schema_change"] for change in changes),
            "provenance_breaking": sum(change["provenance_breaking"] for change in changes),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def _certified_datasets(custody_path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    custody = certify_export_custody(custody_path)
    if not custody["certified"] or custody["export_manifest_path"] is None:
        return None, custody
    imported = import_runtime_export_manifest(custody["export_manifest_path"])
    if not imported["valid"]:
        return None, {**custody, "diagnostics": custody["diagnostics"] + imported["diagnostics"]}
    return imported["datasets"], custody


def build_export_drift_preview(comparison_path: Path) -> dict[str, Any]:
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    root = comparison_path.parent
    baseline_path = (root / comparison["baseline_custody"]).resolve()
    candidate_path = (root / comparison["candidate_custody"]).resolve()
    baseline, baseline_custody = _certified_datasets(baseline_path)
    candidate, candidate_custody = _certified_datasets(candidate_path)
    drift = None
    if baseline is not None and candidate is not None:
        drift = compare_export_datasets(
            baseline, candidate, set(comparison.get("declared_changes") or [])
        )
    passed = bool(drift and drift["certified"])
    return {
        "phase": "PMB-34E",
        "mode": "LOCAL_OFFLINE_EXPORT_DIFFERENTIAL_AND_DRIFT_CERTIFICATION",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "baseline_custody_certified": baseline_custody["certified"],
        "candidate_custody_certified": candidate_custody["certified"],
        "baseline_chain_digest": baseline_custody["chain_digest"],
        "candidate_chain_digest": candidate_custody["chain_digest"],
        "drift": drift,
        "summary": {
            "certification_passed": passed,
            "pmb35_deployment_unblocked": False,
            "reason": "OFFLINE_FIXTURES_ONLY" if passed else "CUSTODY_OR_DRIFT_GATE_FAILED",
        },
    }


def write_export_drift_preview(comparison_path: Path, output_dir: Path) -> Path:
    report = build_export_drift_preview(comparison_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34e_offline_export_drift_certification.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
