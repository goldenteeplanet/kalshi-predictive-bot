"""Build a deterministic forecast-feature dependency map from offline evidence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4da.feature-map-input.v1"
REPORT_SCHEMA = "phase4da.feature-map-report.v1"
TRIGGERS = (
    "EVIDENCE_HASH_CHANGED",
    "FRESHNESS_EXPIRED",
    "SCHEMA_CHANGED",
    "FEATURE_DEPENDENCY_CHANGED",
)
BASE_TRIGGERS = set(TRIGGERS[:3])
MAX_NODES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any, error: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(error)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(error) from exc
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "evidence", "features", "artifact_hash"}:
        raise ValueError("PHASE4DA_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DA_INPUT_SCHEMA_OR_HASH_INVALID")
    evidence = payload.get("evidence")
    features = payload.get("features")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not isinstance(features, list)
        or not features
        or len(evidence) + len(features) > MAX_NODES
    ):
        raise ValueError("PHASE4DA_NODE_COUNT_INVALID")

    evidence_by_id = {}
    for row in evidence:
        required = {"evidence_id", "source", "schema_hash", "artifact_hash", "freshness_limit_ms"}
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("PHASE4DA_EVIDENCE_FIELDS_INVALID")
        identifier = row["evidence_id"]
        if not isinstance(identifier, str) or not identifier or identifier in evidence_by_id:
            raise ValueError("PHASE4DA_EVIDENCE_ID_INVALID")
        if not isinstance(row["source"], str) or not row["source"]:
            raise ValueError("PHASE4DA_EVIDENCE_SOURCE_INVALID")
        _digest(row["schema_hash"], "PHASE4DA_SCHEMA_HASH_INVALID")
        _digest(row["artifact_hash"], "PHASE4DA_ARTIFACT_HASH_INVALID")
        freshness = row["freshness_limit_ms"]
        if isinstance(freshness, bool) or not isinstance(freshness, int) or freshness <= 0:
            raise ValueError("PHASE4DA_FRESHNESS_INVALID")
        evidence_by_id[identifier] = row

    feature_by_id = {}
    for row in features:
        required = {"feature_id", "dependencies", "computation_cost_units", "invalidation_triggers"}
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("PHASE4DA_FEATURE_FIELDS_INVALID")
        identifier = row["feature_id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in feature_by_id
            or identifier in evidence_by_id
        ):
            raise ValueError("PHASE4DA_FEATURE_ID_INVALID")
        dependencies = row["dependencies"]
        if not isinstance(dependencies, list) or not dependencies:
            raise ValueError("PHASE4DA_DEPENDENCIES_INVALID")
        normalized_dependencies = []
        seen_dependencies = set()
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != {"kind", "id"}:
                raise ValueError("PHASE4DA_DEPENDENCY_FIELDS_INVALID")
            pair = (dependency["kind"], dependency["id"])
            if dependency["kind"] not in {"EVIDENCE", "FEATURE"} or pair in seen_dependencies:
                raise ValueError("PHASE4DA_DEPENDENCY_INVALID")
            if not isinstance(dependency["id"], str) or not dependency["id"]:
                raise ValueError("PHASE4DA_DEPENDENCY_INVALID")
            normalized_dependencies.append(dependency)
            seen_dependencies.add(pair)
        cost = row["computation_cost_units"]
        if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
            raise ValueError("PHASE4DA_COST_INVALID")
        triggers = row["invalidation_triggers"]
        if (
            not isinstance(triggers, list)
            or len(set(triggers)) != len(triggers)
            or any(trigger not in TRIGGERS for trigger in triggers)
            or not BASE_TRIGGERS.issubset(triggers)
            or (
                any(item["kind"] == "FEATURE" for item in normalized_dependencies)
                and "FEATURE_DEPENDENCY_CHANGED" not in triggers
            )
        ):
            raise ValueError("PHASE4DA_TRIGGERS_INVALID")
        feature_by_id[identifier] = row

    for row in features:
        for dependency in row["dependencies"]:
            collection = evidence_by_id if dependency["kind"] == "EVIDENCE" else feature_by_id
            if dependency["id"] not in collection:
                raise ValueError("PHASE4DA_DEPENDENCY_MISSING")

    state: dict[str, str] = {}
    order = []

    def visit(identifier: str) -> None:
        if state.get(identifier) == "ACTIVE":
            raise ValueError("PHASE4DA_FEATURE_CYCLE")
        if state.get(identifier) == "DONE":
            return
        state[identifier] = "ACTIVE"
        dependencies = feature_by_id[identifier]["dependencies"]
        for dependency in sorted(dependencies, key=lambda item: (item["kind"], item["id"])):
            if dependency["kind"] == "FEATURE":
                visit(dependency["id"])
        state[identifier] = "DONE"
        order.append(identifier)

    for identifier in sorted(feature_by_id):
        visit(identifier)

    resolved = {}
    for identifier in order:
        row = feature_by_id[identifier]
        upstream = set()
        feature_closure = set()
        for dependency in row["dependencies"]:
            if dependency["kind"] == "EVIDENCE":
                upstream.add(dependency["id"])
            else:
                upstream.update(resolved[dependency["id"]]["upstream_evidence"])
                feature_closure.add(dependency["id"])
                feature_closure.update(resolved[dependency["id"]]["feature_closure"])
        if not upstream:
            raise ValueError("PHASE4DA_UPSTREAM_EVIDENCE_MISSING")
        cumulative_cost = row["computation_cost_units"] + sum(
            feature_by_id[name]["computation_cost_units"] for name in feature_closure
        )
        resolved[identifier] = {
            "feature_id": identifier,
            "direct_dependencies": sorted(
                row["dependencies"], key=lambda item: (item["kind"], item["id"])
            ),
            "upstream_evidence": sorted(upstream),
            "feature_closure": sorted(feature_closure),
            "effective_freshness_limit_ms": min(
                evidence_by_id[name]["freshness_limit_ms"] for name in upstream
            ),
            "direct_computation_cost_units": row["computation_cost_units"],
            "cumulative_computation_cost_units": cumulative_cost,
            "invalidation_triggers": sorted(row["invalidation_triggers"]),
        }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DA",
        "input_hash": payload["artifact_hash"],
        "topological_order": order,
        "features": [resolved[name] for name in order],
        "evidence": [evidence_by_id[name] for name in sorted(evidence_by_id)],
        "forecast_records_created": 0,
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
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.map.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
