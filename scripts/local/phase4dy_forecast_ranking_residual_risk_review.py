"""Build the deterministic Phase 4DY forecast/ranking residual-risk review."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dy.review-input.v1"
REPORT_SCHEMA = "phase4dy.residual-risk-review.v1"
SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "artifact_hash"}
    return canonical_hash(value)


def _items(value: Any, fields: set[str], label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"PHASE4DY_{label}_EMPTY")
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"PHASE4DY_{label}_FIELDS_INVALID")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError(f"PHASE4DY_{label}_ID_INVALID")
        seen.add(identifier)
        for key, item in row.items():
            if key not in {"id", "before_work_units", "after_work_units"} and (
                not isinstance(item, str) or not item.strip()
            ):
                raise ValueError(f"PHASE4DY_{label}_{key.upper()}_INVALID")
    return sorted(value, key=lambda row: row["id"])


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "performance_gains",
        "unsupported_optimizations",
        "model_risks",
        "critical_path_costs",
        "artifact_hash",
    }
    if set(payload) != required or payload.get("schema") != INPUT_SCHEMA:
        raise ValueError("PHASE4DY_INPUT_FIELDS_OR_SCHEMA_INVALID")
    if payload["artifact_hash"] != _hash(payload):
        raise ValueError("PHASE4DY_INPUT_HASH_INVALID")

    gains = _items(
        payload["performance_gains"],
        {"id", "optimization", "metric", "before_work_units", "after_work_units", "evidence"},
        "GAINS",
    )
    for row in gains:
        for field in ("before_work_units", "after_work_units"):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("PHASE4DY_GAIN_WORK_UNITS_INVALID")
        if row["after_work_units"] > row["before_work_units"]:
            raise ValueError("PHASE4DY_GAIN_REGRESSION_INVALID")

    unsupported = _items(
        payload["unsupported_optimizations"],
        {"id", "optimization", "reason", "required_evidence"},
        "UNSUPPORTED",
    )
    risks = _items(
        payload["model_risks"],
        {"id", "risk", "severity", "mitigation", "residual_exposure"},
        "RISKS",
    )
    for row in risks:
        if row["severity"] not in SEVERITIES:
            raise ValueError("PHASE4DY_RISK_SEVERITY_INVALID")
    costs = _items(
        payload["critical_path_costs"],
        {"id", "stage", "cost_driver", "measurement", "next_action"},
        "COSTS",
    )

    reductions = [row["before_work_units"] - row["after_work_units"] for row in gains]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DY",
        "input_hash": payload["artifact_hash"],
        "status": "RESIDUAL_RISK_REVIEW_COMPLETE",
        "performance_gains": gains,
        "unsupported_optimizations": unsupported,
        "model_risks": risks,
        "critical_path_costs": costs,
        "total_deterministic_work_reduction": sum(reductions),
        "high_or_critical_risk_count": sum(
            row["severity"] in {"HIGH", "CRITICAL"} for row in risks
        ),
        "production_deployment_authorized": False,
        "execution_authorized": False,
        "runtime_changes_applied": 0,
        "production_records_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
