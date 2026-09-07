"""Evaluate exact data-stage resource bounds from supplied measurements."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cs.resource-input.v1"
REPORT_SCHEMA = "phase4cs.resource-report.v1"
METRICS = ("pages", "markets", "snapshots", "bytes", "peak_memory_bytes", "elapsed_ms")
MAX_STAGES = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _measurements(value: Any, prefix: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(METRICS):
        raise ValueError(f"PHASE4CS_{prefix}_FIELDS_INVALID")
    for metric in METRICS:
        amount = value[metric]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError(f"PHASE4CS_{prefix}_{metric.upper()}_INVALID")
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "limits", "stages", "artifact_hash"}:
        raise ValueError("PHASE4CS_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CS_INPUT_SCHEMA_OR_HASH_INVALID")
    limits = _measurements(payload.get("limits"), "LIMIT")
    stages = payload.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > MAX_STAGES:
        raise ValueError("PHASE4CS_STAGE_COUNT_INVALID")

    names: set[str] = set()
    cumulative = {metric: 0 for metric in METRICS}
    peak_memory = elapsed = 0
    decisions = []
    halted = False
    halt_stage = None
    halt_metric = None
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict) or set(stage) != {"name", "usage"}:
            raise ValueError("PHASE4CS_STAGE_FIELDS_INVALID")
        name = stage["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("PHASE4CS_STAGE_NAME_INVALID")
        names.add(name)
        usage = _measurements(stage["usage"], "USAGE")
        if usage["peak_memory_bytes"] < peak_memory or usage["elapsed_ms"] < elapsed:
            raise ValueError("PHASE4CS_MONOTONIC_GAUGE_INVALID")
        for metric in ("pages", "markets", "snapshots", "bytes"):
            cumulative[metric] += usage[metric]
        peak_memory = usage["peak_memory_bytes"]
        elapsed = usage["elapsed_ms"]
        cumulative["peak_memory_bytes"] = peak_memory
        cumulative["elapsed_ms"] = elapsed
        exceeded = [metric for metric in METRICS if cumulative[metric] > limits[metric]]
        status = "NOT_EVALUATED_AFTER_REFUSAL" if halted else "REFUSE" if exceeded else "PASS"
        if status == "REFUSE":
            halted = True
            halt_stage = name
            halt_metric = exceeded[0]
        decisions.append(
            {
                "index": index,
                "name": name,
                "status": status,
                "cumulative": dict(cumulative),
                "exceeded_metrics": exceeded,
            }
        )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CS",
        "input_hash": payload["artifact_hash"],
        "status": "REFUSE" if halted else "PASS",
        "halt_stage": halt_stage,
        "halt_metric": halt_metric,
        "limits": limits,
        "stages": decisions,
        "execution_authorized": False,
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
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.measurements.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
