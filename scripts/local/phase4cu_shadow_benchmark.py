"""Replay market-data fixtures through baseline and candidate paths offline."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cu.shadow-fixtures.v1"
ATTESTATION_SCHEMA = "phase4cu.prior-attestation.v1"
REPORT_SCHEMA = "phase4cu.shadow-comparison.v1"
MAX_FIXTURES = 1_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _component_status(component: Any) -> str:
    if not isinstance(component, dict) or set(component) != {"sequence", "records"}:
        return "INVALID"
    sequence, records = component["sequence"], component["records"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        return "INVALID"
    if not isinstance(records, list):
        return "INVALID"
    keys = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"key", "value"}:
            return "INVALID"
        key, value = record["key"], record["value"]
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not isinstance(key, str) or not key or key in keys or not numeric:
            return "INVALID"
        if not math.isfinite(value):
            return "INVALID"
        keys.add(key)
    return "PASS"


def prior_attestation(components: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": ATTESTATION_SCHEMA,
        "snapshot_hash": canonical_hash(components),
        "component_hashes": {
            name: canonical_hash(value) for name, value in sorted(components.items())
        },
        "component_statuses": {
            name: _component_status(value) for name, value in sorted(components.items())
        },
    }
    result["artifact_hash"] = _hash(result)
    return result


def _output(components: dict[str, Any], statuses: dict[str, str]) -> dict[str, Any]:
    return {
        "snapshot_hash": canonical_hash(components),
        "component_statuses": statuses,
        "verdict": "PASS" if all(value == "PASS" for value in statuses.values()) else "REFUSE",
    }


def _baseline(components: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    statuses = {name: _component_status(value) for name, value in sorted(components.items())}
    serialized = json.dumps(components, sort_keys=True, indent=2, ensure_ascii=False).encode()
    return _output(components, statuses), {
        "validated_components": len(components),
        "serialized_bytes": len(serialized),
        "work_units": len(components) * 100 + len(serialized),
    }


def _candidate(
    previous: dict[str, Any], current: dict[str, Any], attestation: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int]]:
    valid_attestation = (
        attestation.get("schema") == ATTESTATION_SCHEMA
        and attestation.get("artifact_hash") == _hash(attestation)
        and attestation.get("snapshot_hash") == canonical_hash(previous)
        and attestation.get("component_hashes")
        == {name: canonical_hash(value) for name, value in sorted(previous.items())}
        and set(attestation.get("component_statuses", {})) == set(previous)
    )
    if not valid_attestation:
        raise ValueError("PHASE4CU_PRIOR_ATTESTATION_INVALID")
    changed = sorted(
        name for name in current if canonical_hash(previous[name]) != canonical_hash(current[name])
    )
    statuses = dict(attestation["component_statuses"])
    for name in changed:
        statuses[name] = _component_status(current[name])
    serialized = json.dumps(
        current, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return _output(current, statuses), {
        "validated_components": len(changed),
        "serialized_bytes": len(serialized),
        "work_units": len(changed) * 100 + len(serialized),
    }


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "fixtures", "artifact_hash"}:
        raise ValueError("PHASE4CU_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CU_INPUT_SCHEMA_OR_HASH_INVALID")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures or len(fixtures) > MAX_FIXTURES:
        raise ValueError("PHASE4CU_FIXTURE_COUNT_INVALID")
    names: set[str] = set()
    comparisons = []
    for fixture in fixtures:
        required = {"fixture_id", "class", "previous", "current", "prior_attestation"}
        if not isinstance(fixture, dict) or set(fixture) != required:
            raise ValueError("PHASE4CU_FIXTURE_FIELDS_INVALID")
        identifier, fixture_class = fixture["fixture_id"], fixture["class"]
        previous, current = fixture["previous"], fixture["current"]
        if not isinstance(identifier, str) or not identifier or identifier in names:
            raise ValueError("PHASE4CU_FIXTURE_ID_INVALID")
        if not isinstance(fixture_class, str) or not fixture_class:
            raise ValueError("PHASE4CU_FIXTURE_CLASS_INVALID")
        if (
            not isinstance(previous, dict)
            or not previous
            or not isinstance(current, dict)
            or set(previous) != set(current)
        ):
            raise ValueError("PHASE4CU_COMPONENT_SET_INVALID")
        names.add(identifier)
        old_output, old_cost = _baseline(current)
        new_output, new_cost = _candidate(previous, current, fixture["prior_attestation"])
        equivalent = old_output == new_output
        nonregressing = new_cost["work_units"] <= old_cost["work_units"]
        comparisons.append(
            {
                "fixture_id": identifier,
                "class": fixture_class,
                "status": "PASS" if equivalent and nonregressing else "FAIL",
                "output_hash": canonical_hash(old_output) if equivalent else None,
                "outputs_equivalent": equivalent,
                "baseline": old_cost,
                "candidate": new_cost,
                "work_units_saved": old_cost["work_units"] - new_cost["work_units"],
            }
        )
    strict = sum(row["work_units_saved"] > 0 for row in comparisons)
    passed = all(row["status"] == "PASS" for row in comparisons) and strict > 0
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CU",
        "input_hash": payload["artifact_hash"],
        "status": "SHADOW_BENCHMARK_PASS" if passed else "REFUSE",
        "comparisons": comparisons,
        "strict_improvement_count": strict,
        "total_work_units_saved": sum(row["work_units_saved"] for row in comparisons),
        "wall_clock_used_for_verdict": False,
        "runtime_changes_applied": 0,
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
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.fixtures.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
