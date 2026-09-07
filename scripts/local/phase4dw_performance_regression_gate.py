"""Gate deterministic performance metrics without flaky wall-clock thresholds."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dw.regression-input.v1"
REPORT_SCHEMA = "phase4dw.regression-report.v1"
METRICS = ("work_units", "allocation_units", "parse_units")
MAX_VALUE = 10**15


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DW_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DW_HASH_INVALID") from exc
    return value


def _nonnegative(value: Any, error: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_VALUE:
        raise ValueError(error)
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "baseline", "current", "thresholds", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DW_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DW_INPUT_SCHEMA_OR_HASH_INVALID")
    snapshot_fields = {*METRICS, "logical_output_hash"}
    baseline, current = payload["baseline"], payload["current"]
    if not isinstance(baseline, dict) or set(baseline) != snapshot_fields:
        raise ValueError("PHASE4DW_BASELINE_FIELDS_INVALID")
    if not isinstance(current, dict) or set(current) != snapshot_fields:
        raise ValueError("PHASE4DW_CURRENT_FIELDS_INVALID")
    for snapshot in (baseline, current):
        _digest(snapshot["logical_output_hash"])
        for metric in METRICS:
            _nonnegative(snapshot[metric], "PHASE4DW_METRIC_INVALID")
    thresholds = payload["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != set(METRICS):
        raise ValueError("PHASE4DW_THRESHOLD_FIELDS_INVALID")
    metric_results = []
    failures = []
    for metric in METRICS:
        policy = thresholds[metric]
        if not isinstance(policy, dict) or set(policy) != {
            "max_absolute_increase",
            "max_percent_increase_basis_points",
        }:
            raise ValueError("PHASE4DW_THRESHOLD_POLICY_FIELDS_INVALID")
        absolute = _nonnegative(policy["max_absolute_increase"], "PHASE4DW_THRESHOLD_INVALID")
        basis_points = _nonnegative(
            policy["max_percent_increase_basis_points"], "PHASE4DW_THRESHOLD_INVALID"
        )
        if basis_points > 1_000_000:
            raise ValueError("PHASE4DW_THRESHOLD_INVALID")
        before, after = baseline[metric], current[metric]
        increase = max(0, after - before)
        absolute_pass = increase <= absolute
        percent_pass = increase == 0 if before == 0 else increase * 10_000 <= before * basis_points
        passed = absolute_pass and percent_pass
        if not passed:
            failures.append(f"{metric.upper()}_REGRESSION")
        metric_results.append(
            {
                "metric": metric,
                "baseline": before,
                "current": after,
                "increase": increase,
                "absolute_pass": absolute_pass,
                "percent_pass": percent_pass,
                "passed": passed,
            }
        )
    output_equal = baseline["logical_output_hash"] == current["logical_output_hash"]
    if not output_equal:
        failures.append("LOGICAL_OUTPUT_CHANGED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DW",
        "input_hash": payload["artifact_hash"],
        "status": "PASS" if not failures else "FAIL",
        "metric_results": metric_results,
        "logical_output_equal": output_equal,
        "failures": failures,
        "wall_clock_used_as_gate": False,
        "production_records_created": 0,
        "execution_authorized": False,
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
