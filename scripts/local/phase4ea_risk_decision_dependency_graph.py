"""Build a deterministic Phase 3M/3N risk-decision dependency graph."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ea.graph-input.v1"
REPORT_SCHEMA = "phase4ea.graph-report.v1"
DECISIONS = {"POSITION_SIZING": "3M", "ADVANCED_RISK": "3N"}
NODE_KINDS = {"CALCULATION", "CAP", "HARD_BLOCK", "DECISION"}
MAX_NODES = 100_000


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "inputs", "nodes", "artifact_hash"}:
        raise ValueError("PHASE4EA_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EA_INPUT_SCHEMA_OR_HASH_INVALID")
    inputs = payload.get("inputs")
    nodes = payload.get("nodes")
    if not isinstance(inputs, list) or not inputs or not isinstance(nodes, list) or not nodes:
        raise ValueError("PHASE4EA_GRAPH_EMPTY")
    if len(inputs) + len(nodes) > MAX_NODES:
        raise ValueError("PHASE4EA_GRAPH_TOO_LARGE")

    input_by_id = {}
    for row in inputs:
        if not isinstance(row, dict) or set(row) != {
            "input_id",
            "source_phase",
            "artifact_hash",
            "immutable",
        }:
            raise ValueError("PHASE4EA_INPUT_NODE_FIELDS_INVALID")
        identifier = row["input_id"]
        if not isinstance(identifier, str) or not identifier or identifier in input_by_id:
            raise ValueError("PHASE4EA_INPUT_ID_INVALID")
        if not isinstance(row["source_phase"], str) or not row["source_phase"]:
            raise ValueError("PHASE4EA_SOURCE_PHASE_INVALID")
        if not isinstance(row["artifact_hash"], str) or len(row["artifact_hash"]) != 64:
            raise ValueError("PHASE4EA_LINEAGE_HASH_INVALID")
        try:
            int(row["artifact_hash"], 16)
        except ValueError as exc:
            raise ValueError("PHASE4EA_LINEAGE_HASH_INVALID") from exc
        if row["immutable"] is not True:
            raise ValueError("PHASE4EA_INPUT_NOT_IMMUTABLE")
        input_by_id[identifier] = row

    node_by_id = {}
    for row in nodes:
        fields = {"node_id", "kind", "decision", "dependencies", "work_units"}
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("PHASE4EA_NODE_FIELDS_INVALID")
        identifier = row["node_id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in node_by_id
            or identifier in input_by_id
        ):
            raise ValueError("PHASE4EA_NODE_ID_INVALID")
        if row["kind"] not in NODE_KINDS:
            raise ValueError("PHASE4EA_NODE_KIND_INVALID")
        if row["kind"] == "DECISION":
            if row["decision"] not in DECISIONS:
                raise ValueError("PHASE4EA_DECISION_INVALID")
        elif row["decision"] is not None:
            raise ValueError("PHASE4EA_NONDECISION_LABEL_INVALID")
        dependencies = row["dependencies"]
        if (
            not isinstance(dependencies, list)
            or not dependencies
            or len(dependencies) != len(set(dependencies))
        ):
            raise ValueError("PHASE4EA_DEPENDENCIES_INVALID")
        if any(not isinstance(item, str) or not item for item in dependencies):
            raise ValueError("PHASE4EA_DEPENDENCIES_INVALID")
        work = row["work_units"]
        if isinstance(work, bool) or not isinstance(work, int) or work < 0:
            raise ValueError("PHASE4EA_WORK_INVALID")
        node_by_id[identifier] = row

    decisions = [row for row in nodes if row["kind"] == "DECISION"]
    if {row["decision"] for row in decisions} != set(DECISIONS) or len(decisions) != 2:
        raise ValueError("PHASE4EA_DECISION_SET_INVALID")
    for row in nodes:
        if any(
            dependency not in input_by_id and dependency not in node_by_id
            for dependency in row["dependencies"]
        ):
            raise ValueError("PHASE4EA_DEPENDENCY_MISSING")

    state: dict[str, str] = {}
    order: list[str] = []

    def visit(identifier: str) -> None:
        if state.get(identifier) == "ACTIVE":
            raise ValueError("PHASE4EA_GRAPH_CYCLE")
        if state.get(identifier) == "DONE":
            return
        state[identifier] = "ACTIVE"
        for dependency in sorted(node_by_id[identifier]["dependencies"]):
            if dependency in node_by_id:
                visit(dependency)
        state[identifier] = "DONE"
        order.append(identifier)

    for identifier in sorted(node_by_id):
        visit(identifier)

    resolved = {}
    for identifier in order:
        row = node_by_id[identifier]
        upstream_inputs = set()
        ancestors = set()
        for dependency in row["dependencies"]:
            if dependency in input_by_id:
                upstream_inputs.add(dependency)
            else:
                ancestors.add(dependency)
                ancestors.update(resolved[dependency]["ancestors"])
                upstream_inputs.update(resolved[dependency]["upstream_inputs"])
        resolved[identifier] = {
            **row,
            "dependencies": sorted(row["dependencies"]),
            "ancestors": sorted(ancestors),
            "upstream_inputs": sorted(upstream_inputs),
            "cumulative_work_units": row["work_units"]
            + sum(node_by_id[name]["work_units"] for name in ancestors),
        }

    decision_rows = {row["decision"]: resolved[row["node_id"]] for row in decisions}
    for decision, row in decision_rows.items():
        ancestor_kinds = {node_by_id[name]["kind"] for name in row["ancestors"]}
        if not {"CAP", "HARD_BLOCK"}.issubset(ancestor_kinds) or not row["upstream_inputs"]:
            raise ValueError(f"PHASE4EA_{decision}_CONTROL_DEPENDENCY_MISSING")
    shared = sorted(
        set(decision_rows["POSITION_SIZING"]["ancestors"])
        & set(decision_rows["ADVANCED_RISK"]["ancestors"])
    )
    shared_calculations = [name for name in shared if node_by_id[name]["kind"] == "CALCULATION"]

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EA",
        "input_hash": payload["artifact_hash"],
        "topological_order": order,
        "inputs": [input_by_id[name] for name in sorted(input_by_id)],
        "nodes": [resolved[name] for name in order],
        "decision_lineage": [decision_rows[name] for name in sorted(decision_rows)],
        "shared_calculations": shared_calculations,
        "position_sizing_phase": DECISIONS["POSITION_SIZING"],
        "advanced_risk_phase": DECISIONS["ADVANCED_RISK"],
        "risk_decisions_created": 0,
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
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.graph.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
