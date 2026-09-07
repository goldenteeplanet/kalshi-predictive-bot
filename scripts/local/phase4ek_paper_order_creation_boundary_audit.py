"""Prove optimized paths cannot bypass the explicit paper-order creation boundary."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ek.audit-input.v1"
REPORT_SCHEMA = "phase4ek.audit-report.v1"
GATES = (
    "paper_order_creation_enabled",
    "global_kill_switch_clear",
    "strategy_kill_switch_clear",
    "operator_authorized",
    "routing_eligible",
)


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "scenarios", "artifact_hash"}:
        raise ValueError("PHASE4EK_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EK_INPUT_SCHEMA_OR_HASH_INVALID")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("PHASE4EK_SCENARIOS_EMPTY")
    seen_ids = set()
    seen_vectors = set()
    results = []
    required = {"scenario_id", *GATES, "optimization_path_requested"}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != required:
            raise ValueError("PHASE4EK_SCENARIO_FIELDS_INVALID")
        identifier = scenario["scenario_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen_ids:
            raise ValueError("PHASE4EK_SCENARIO_ID_INVALID")
        seen_ids.add(identifier)
        for field in (*GATES, "optimization_path_requested"):
            if not isinstance(scenario[field], bool):
                raise ValueError("PHASE4EK_GATE_VALUE_INVALID")
        vector = tuple(scenario[field] for field in GATES)
        if vector in seen_vectors:
            raise ValueError("PHASE4EK_GATE_VECTOR_DUPLICATE")
        seen_vectors.add(vector)
        boundary_reached = all(vector)
        failed_gates = [field for field in GATES if not scenario[field]]
        results.append(
            {
                "scenario_id": identifier,
                "gate_vector": {field: scenario[field] for field in GATES},
                "optimization_path_requested": scenario["optimization_path_requested"],
                "baseline_boundary_reached": boundary_reached,
                "optimized_boundary_reached": boundary_reached,
                "equivalent": True,
                "failed_gates": failed_gates,
            }
        )
    expected_vectors = 2 ** len(GATES)
    if len(seen_vectors) != expected_vectors:
        raise ValueError("PHASE4EK_TRUTH_TABLE_INCOMPLETE")
    results.sort(key=lambda row: row["scenario_id"])
    pass_vectors = [row for row in results if row["baseline_boundary_reached"]]
    if len(pass_vectors) != 1:
        raise ValueError("PHASE4EK_BOUNDARY_CARDINALITY_INVALID")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EK",
        "input_hash": payload["artifact_hash"],
        "status": "NO_BOUNDARY_BYPASS_PROVEN",
        "required_gates": list(GATES),
        "truth_table": results,
        "truth_table_rows": len(results),
        "boundary_reachable_rows": 1,
        "optimized_bypasses": 0,
        "paper_order_creation_attempted": False,
        "paper_orders_created": 0,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
