"""Perform offline incremental feature recomputation with baseline equivalence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, DivisionByZero, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dc.incremental-input.v1"
REPORT_SCHEMA = "phase4dc.incremental-report.v1"
OPS = {"SOURCE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "MIN", "MAX"}
MAX_NODES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DC_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DC_DECIMAL_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DC_DECIMAL_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def _validate(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    required = {"schema", "nodes", "previous_values", "source_updates", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DC_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DC_INPUT_SCHEMA_OR_HASH_INVALID")
    nodes = payload["nodes"]
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_NODES:
        raise ValueError("PHASE4DC_NODE_COUNT_INVALID")
    identifiers: set[str] = set()
    normalized = []
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "op", "dependencies", "source_value"}:
            raise ValueError("PHASE4DC_NODE_FIELDS_INVALID")
        identifier, op, dependencies = node["id"], node["op"], node["dependencies"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DC_NODE_ID_INVALID")
        if (
            op not in OPS
            or not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError("PHASE4DC_NODE_DEFINITION_INVALID")
        if op == "SOURCE":
            if dependencies:
                raise ValueError("PHASE4DC_SOURCE_DEPENDENCIES_INVALID")
            _decimal(node["source_value"])
        elif (
            node["source_value"] is not None
            or (op in {"SUBTRACT", "DIVIDE"} and len(dependencies) != 2)
            or (op in {"ADD", "MULTIPLY", "MIN", "MAX"} and not dependencies)
        ):
            raise ValueError("PHASE4DC_NODE_ARITY_INVALID")
        identifiers.add(identifier)
        normalized.append(node)
    previous, updates = payload["previous_values"], payload["source_updates"]
    if not isinstance(previous, dict) or set(previous) != identifiers:
        raise ValueError("PHASE4DC_PREVIOUS_VALUES_INVALID")
    if not isinstance(updates, dict) or not updates:
        raise ValueError("PHASE4DC_SOURCE_UPDATES_INVALID")
    for key, value in {**previous, **updates}.items():
        if not isinstance(key, str):
            raise ValueError("PHASE4DC_VALUE_KEY_INVALID")
        _decimal(value)
    source_ids = {node["id"] for node in normalized if node["op"] == "SOURCE"}
    if not set(updates) <= source_ids:
        raise ValueError("PHASE4DC_UPDATE_TARGET_INVALID")
    return normalized, previous, updates


def _topological(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {node["id"]: node for node in nodes}
    if any(dependency not in by_id for node in nodes for dependency in node["dependencies"]):
        raise ValueError("PHASE4DC_DEPENDENCY_MISSING")
    order: list[dict[str, Any]] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError("PHASE4DC_DEPENDENCY_CYCLE")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier]["dependencies"]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
        order.append(by_id[identifier])

    for identifier in sorted(by_id):
        visit(identifier)
    return order


def _calculate(op: str, operands: list[Decimal]) -> Decimal:
    try:
        if op == "ADD":
            return sum(operands, Decimal(0))
        if op == "SUBTRACT":
            return operands[0] - operands[1]
        if op == "MULTIPLY":
            result = Decimal(1)
            for value in operands:
                result *= value
            return result
        if op == "DIVIDE":
            return operands[0] / operands[1]
        if op == "MIN":
            return min(operands)
        if op == "MAX":
            return max(operands)
    except (DivisionByZero, InvalidOperation) as exc:
        raise ValueError("PHASE4DC_COMPUTATION_INVALID") from exc
    raise ValueError("PHASE4DC_OPERATION_INVALID")


def _full(order: list[dict[str, Any]], updates: dict[str, str]) -> dict[str, str]:
    values: dict[str, Decimal] = {}
    for node in order:
        if node["op"] == "SOURCE":
            values[node["id"]] = _decimal(updates.get(node["id"], node["source_value"]))
        else:
            values[node["id"]] = _calculate(
                node["op"], [values[item] for item in node["dependencies"]]
            )
    return {identifier: _render(value) for identifier, value in values.items()}


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    nodes, previous, updates = _validate(payload)
    order = _topological(nodes)
    baseline_before = _full(order, {})
    if previous != baseline_before:
        raise ValueError("PHASE4DC_PREVIOUS_BASELINE_MISMATCH")
    baseline_after = _full(order, updates)
    reverse: dict[str, set[str]] = {node["id"]: set() for node in nodes}
    for node in nodes:
        for dependency in node["dependencies"]:
            reverse[dependency].add(node["id"])
    affected = set(updates)
    frontier = list(updates)
    while frontier:
        current = frontier.pop()
        for dependent in reverse[current]:
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    incremental = dict(previous)
    by_id = {node["id"]: node for node in nodes}
    for node in order:
        if node["id"] not in affected:
            continue
        if node["op"] == "SOURCE":
            incremental[node["id"]] = _render(_decimal(updates[node["id"]]))
        else:
            incremental[node["id"]] = _render(
                _calculate(
                    node["op"],
                    [_decimal(incremental[item]) for item in node["dependencies"]],
                )
            )
    if incremental != baseline_after:
        raise ValueError("PHASE4DC_INCREMENTAL_EQUIVALENCE_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DC",
        "input_hash": payload["artifact_hash"],
        "updated_source_ids": sorted(updates),
        "recomputed_node_ids": [node["id"] for node in order if node["id"] in affected],
        "reused_node_ids": sorted(set(by_id) - affected),
        "values": incremental,
        "baseline_values_hash": canonical_hash(baseline_after),
        "incremental_values_hash": canonical_hash(incremental),
        "full_equivalence": True,
        "production_records_created": 0,
        "forecast_records_created": 0,
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
