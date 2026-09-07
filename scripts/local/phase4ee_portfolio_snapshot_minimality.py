"""Derive the smallest sufficient immutable portfolio snapshot for risk evaluation."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ee.minimality-input.v1"
REPORT_SCHEMA = "phase4ee.minimality-report.v1"
DECISIONS = {"POSITION_SIZING", "ADVANCED_RISK"}
MAX_FIELDS = 20


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "fields", "requirements", "artifact_hash"}:
        raise ValueError("PHASE4EE_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EE_INPUT_SCHEMA_OR_HASH_INVALID")
    fields = payload.get("fields")
    requirements = payload.get("requirements")
    if not isinstance(fields, list) or not fields or len(fields) > MAX_FIELDS:
        raise ValueError("PHASE4EE_FIELD_COUNT_INVALID")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("PHASE4EE_REQUIREMENTS_EMPTY")
    field_by_id = {}
    for row in fields:
        if not isinstance(row, dict) or set(row) != {
            "field_id",
            "value_hash",
            "source_artifact_hash",
            "immutable",
        }:
            raise ValueError("PHASE4EE_FIELD_FIELDS_INVALID")
        identifier = row["field_id"]
        if not isinstance(identifier, str) or not identifier or identifier in field_by_id:
            raise ValueError("PHASE4EE_FIELD_ID_INVALID")
        if not _digest(row["value_hash"]) or not _digest(row["source_artifact_hash"]):
            raise ValueError("PHASE4EE_FIELD_HASH_INVALID")
        if row["immutable"] is not True:
            raise ValueError("PHASE4EE_FIELD_NOT_IMMUTABLE")
        field_by_id[identifier] = row
    seen_requirements = set()
    normalized_requirements = []
    covered_decisions = set()
    for row in requirements:
        if not isinstance(row, dict) or set(row) != {
            "requirement_id",
            "decision",
            "acceptable_fields",
        }:
            raise ValueError("PHASE4EE_REQUIREMENT_FIELDS_INVALID")
        identifier = row["requirement_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen_requirements:
            raise ValueError("PHASE4EE_REQUIREMENT_ID_INVALID")
        seen_requirements.add(identifier)
        if row["decision"] not in DECISIONS:
            raise ValueError("PHASE4EE_DECISION_INVALID")
        covered_decisions.add(row["decision"])
        acceptable = row["acceptable_fields"]
        if (
            not isinstance(acceptable, list)
            or not acceptable
            or len(acceptable) != len(set(acceptable))
        ):
            raise ValueError("PHASE4EE_ACCEPTABLE_FIELDS_INVALID")
        if any(item not in field_by_id for item in acceptable):
            raise ValueError("PHASE4EE_ACCEPTABLE_FIELD_MISSING")
        normalized_requirements.append({**row, "acceptable_fields": sorted(acceptable)})
    if covered_decisions != DECISIONS:
        raise ValueError("PHASE4EE_DECISION_COVERAGE_INVALID")
    normalized_requirements.sort(key=lambda row: row["requirement_id"])

    identifiers = sorted(field_by_id)
    selected: tuple[str, ...] | None = None
    for size in range(1, len(identifiers) + 1):
        for combination in itertools.combinations(identifiers, size):
            chosen = set(combination)
            if all(
                chosen.intersection(row["acceptable_fields"]) for row in normalized_requirements
            ):
                selected = combination
                break
        if selected is not None:
            break
    if selected is None:
        raise ValueError("PHASE4EE_REQUIREMENTS_UNSATISFIABLE")
    selected_set = set(selected)
    coverage = [
        {
            "requirement_id": row["requirement_id"],
            "decision": row["decision"],
            "selected_fields": sorted(selected_set.intersection(row["acceptable_fields"])),
        }
        for row in normalized_requirements
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EE",
        "input_hash": payload["artifact_hash"],
        "status": "MINIMUM_SUFFICIENT_SNAPSHOT_PROVEN",
        "selected_fields": [field_by_id[name] for name in selected],
        "selected_field_ids": list(selected),
        "omitted_field_ids": [name for name in identifiers if name not in selected_set],
        "requirements": normalized_requirements,
        "coverage": coverage,
        "minimum_field_count": len(selected),
        "tie_breaker": "LEXICOGRAPHIC_FIELD_IDS",
        "portfolio_reads_per_evaluation": 0,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
