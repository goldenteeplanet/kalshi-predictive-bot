from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from kalshi_predictor.benchmarking.export_custody import certify_export_custody
from kalshi_predictor.benchmarking.export_drift import compare_export_datasets
from kalshi_predictor.benchmarking.runtime_export_import import import_runtime_export_manifest


MIN_BUNDLES = 3
MAX_BUNDLES = 20
SEVERITY_ORDER = {"INFO": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _alert_for_change(change: Mapping[str, Any], recurrence: int) -> dict[str, Any] | None:
    if change["provenance_breaking"] and not change["explained"]:
        severity, reason = "CRITICAL", "UNEXPLAINED_PROVENANCE_BREAK"
    elif change["schema_change"] and not change["explained"]:
        severity, reason = "HIGH", "UNEXPLAINED_SCHEMA_DRIFT"
    elif recurrence >= 2:
        severity, reason = "MEDIUM", "RECURRING_FIELD_DRIFT"
    elif change["explained"]:
        severity, reason = "INFO", "DECLARED_CHANGE"
    else:
        severity, reason = "MEDIUM", "UNEXPLAINED_VALUE_DRIFT"
    return {
        "severity": severity,
        "reason": reason,
        "change_id": change["change_id"],
        "dataset": change["dataset"],
        "field": change["field"],
        "recurrence": recurrence,
    }


def analyze_dataset_trend(
    snapshots: Sequence[Mapping[str, list[Mapping[str, Any]]]],
    declarations: Sequence[set[str]] | None = None,
) -> dict[str, Any]:
    declared = declarations or [set() for _ in range(max(0, len(snapshots) - 1))]
    if len(snapshots) < MIN_BUNDLES or len(snapshots) > MAX_BUNDLES:
        return {
            "certified": False,
            "diagnostics": [f"BUNDLE_COUNT_OUT_OF_RANGE:{len(snapshots)}:{MIN_BUNDLES}-{MAX_BUNDLES}"],
            "transitions": [], "alerts": [], "recurring_drift": [],
        }
    if len(declared) != len(snapshots) - 1:
        return {
            "certified": False,
            "diagnostics": ["DECLARATION_TRANSITION_COUNT_MISMATCH"],
            "transitions": [], "alerts": [], "recurring_drift": [],
        }
    transitions = [
        compare_export_datasets(snapshots[index], snapshots[index + 1], declared[index])
        for index in range(len(snapshots) - 1)
    ]
    recurrence: dict[str, int] = {}
    for transition in transitions:
        for change in transition["changes"]:
            signature = f"{change['dataset']}:{change['field']}"
            recurrence[signature] = recurrence.get(signature, 0) + 1
    alerts = []
    for transition_index, transition in enumerate(transitions):
        for change in transition["changes"]:
            signature = f"{change['dataset']}:{change['field']}"
            alert = _alert_for_change(change, recurrence[signature])
            alert["transition_index"] = transition_index
            alerts.append(alert)
    alerts.sort(key=lambda row: (-SEVERITY_ORDER[row["severity"]], row["transition_index"], row["change_id"]))
    recurring = [
        {"signature": signature, "occurrences": count}
        for signature, count in sorted(recurrence.items()) if count >= 2
    ]
    diagnostics = [
        f"TRANSITION_{index}:{diagnostic}"
        for index, transition in enumerate(transitions)
        for diagnostic in transition["diagnostics"]
    ]
    canonical = json.dumps({"transitions": transitions, "alerts": alerts}, sort_keys=True, separators=(",", ":")).encode()
    return {
        "certified": not diagnostics,
        "diagnostics": diagnostics,
        "transitions": transitions,
        "alerts": alerts,
        "recurring_drift": recurring,
        "summary": {
            "bundles": len(snapshots),
            "transitions": len(transitions),
            "changes": sum(item["summary"]["changes"] for item in transitions),
            "alerts": len(alerts),
            "critical": sum(row["severity"] == "CRITICAL" for row in alerts),
            "high": sum(row["severity"] == "HIGH" for row in alerts),
            "medium": sum(row["severity"] == "MEDIUM" for row in alerts),
            "info": sum(row["severity"] == "INFO" for row in alerts),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def build_export_drift_trend_preview(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent.resolve()
    bundle_specs = manifest.get("bundles") or []
    diagnostics: list[str] = []
    snapshots = []
    custody_rows = []
    observed_timestamps = []
    for index, spec in enumerate(bundle_specs):
        timestamp = spec.get("observed_at")
        if timestamp in observed_timestamps:
            diagnostics.append(f"DUPLICATE_BUNDLE_TIMESTAMP:{timestamp}")
        observed_timestamps.append(timestamp)
        custody_path = (root / str(spec.get("custody", ""))).resolve()
        custody = certify_export_custody(custody_path)
        custody_rows.append({
            "index": index, "observed_at": timestamp,
            "certified": custody["certified"], "chain_digest": custody["chain_digest"],
        })
        if not custody["certified"] or custody["export_manifest_path"] is None:
            diagnostics.append(f"BUNDLE_CUSTODY_FAILED:{index}")
            continue
        imported = import_runtime_export_manifest(custody["export_manifest_path"])
        if not imported["valid"]:
            diagnostics.append(f"BUNDLE_IMPORT_FAILED:{index}")
            continue
        snapshots.append(imported["datasets"])
    if observed_timestamps != sorted(observed_timestamps):
        diagnostics.append("BUNDLE_TIMESTAMPS_NOT_STRICTLY_ORDERED")
    declarations = [set(values) for values in (manifest.get("declared_changes") or [])]
    trend = analyze_dataset_trend(snapshots, declarations) if not diagnostics else None
    passed = bool(trend and trend["certified"] and not diagnostics)
    return {
        "phase": "PMB-34F",
        "mode": "LOCAL_OFFLINE_MULTI_BUNDLE_DRIFT_TREND_ALERT_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "bundle_bounds": {"minimum": MIN_BUNDLES, "maximum": MAX_BUNDLES},
        "custody_bundles": custody_rows,
        "series_diagnostics": diagnostics,
        "trend": trend,
        "summary": {
            "certification_passed": passed,
            "pmb35_deployment_unblocked": False,
            "reason": "OFFLINE_FIXTURES_ONLY" if passed else "SERIES_OR_DRIFT_GATE_FAILED",
        },
    }


def write_export_drift_trend_preview(manifest_path: Path, output_dir: Path) -> Path:
    report = build_export_drift_trend_preview(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34f_offline_multi_bundle_drift_trend.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
