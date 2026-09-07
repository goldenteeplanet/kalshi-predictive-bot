"""Prove incremental snapshot validation equivalent to periodic full validation offline."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cq.incremental-input.v1"
ATTESTATION_SCHEMA = "phase4cq.full-attestation.v1"
REPORT_SCHEMA = "phase4cq.incremental-report.v1"
MAX_COMPONENTS = 100


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4CQ_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4CQ_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4CQ_TIMESTAMP_INVALID")
    return parsed


def _ms(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _validate_component(component: Any) -> str:
    if not isinstance(component, dict) or set(component) != {"sequence", "captured_at", "records"}:
        return "INVALID_SHAPE"
    sequence = component["sequence"]
    records = component["records"]
    try:
        _time(component["captured_at"])
    except ValueError:
        return "INVALID_TIMESTAMP"
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        return "INVALID_SEQUENCE"
    if not isinstance(records, list):
        return "INVALID_RECORDS"
    keys = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"key", "value"}:
            return "INVALID_RECORD"
        key, value = record["key"], record["value"]
        if not isinstance(key, str) or not key or key in keys:
            return "INVALID_RECORD_KEY"
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not numeric or not math.isfinite(value):
            return "INVALID_RECORD_VALUE"
        keys.add(key)
    return "PASS"


def _snapshot_status(components: dict[str, Any], statuses: dict[str, str], max_skew: int) -> str:
    if any(status != "PASS" for status in statuses.values()):
        return "INVALID_COMPONENT"
    timestamps = [_time(component["captured_at"]) for component in components.values()]
    if _ms(max(timestamps), min(timestamps)) > max_skew:
        return "INCOHERENT_COMPONENT_TIME"
    return "PASS"


def build_attestation(components: dict[str, Any]) -> dict[str, Any]:
    statuses = {name: _validate_component(value) for name, value in sorted(components.items())}
    attestation: dict[str, Any] = {
        "schema": ATTESTATION_SCHEMA,
        "snapshot_hash": canonical_hash(components),
        "component_hashes": {
            name: canonical_hash(value) for name, value in sorted(components.items())
        },
        "component_statuses": statuses,
    }
    attestation["artifact_hash"] = _hash(attestation)
    return attestation


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "full_validation_interval",
        "cycles_since_full",
        "max_component_skew_ms",
        "previous_components",
        "current_components",
        "prior_full_attestation",
        "artifact_hash",
    }
    if set(payload) != fields:
        raise ValueError("PHASE4CQ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CQ_INPUT_SCHEMA_OR_HASH_INVALID")
    interval = payload.get("full_validation_interval")
    cycles = payload.get("cycles_since_full")
    max_skew = payload.get("max_component_skew_ms")
    for value, error in (
        (interval, "PHASE4CQ_INTERVAL_INVALID"),
        (max_skew, "PHASE4CQ_MAX_SKEW_INVALID"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(error)
    if isinstance(cycles, bool) or not isinstance(cycles, int) or not 0 <= cycles < interval:
        raise ValueError("PHASE4CQ_CYCLES_INVALID")
    previous = payload.get("previous_components")
    current = payload.get("current_components")
    if (
        not isinstance(previous, dict)
        or not previous
        or not isinstance(current, dict)
        or set(previous) != set(current)
        or len(current) > MAX_COMPONENTS
        or any(not isinstance(name, str) or not name for name in current)
    ):
        raise ValueError("PHASE4CQ_COMPONENT_SET_INVALID")
    attestation = payload.get("prior_full_attestation")
    if (
        not isinstance(attestation, dict)
        or attestation.get("schema") != ATTESTATION_SCHEMA
        or attestation.get("artifact_hash") != _hash(attestation)
        or attestation.get("snapshot_hash") != canonical_hash(previous)
        or attestation.get("component_hashes")
        != {name: canonical_hash(value) for name, value in sorted(previous.items())}
        or set(attestation.get("component_statuses", {})) != set(previous)
    ):
        raise ValueError("PHASE4CQ_PRIOR_ATTESTATION_INVALID")

    changed = sorted(
        name for name in current if canonical_hash(current[name]) != canonical_hash(previous[name])
    )
    full_due = cycles + 1 >= interval
    validated = sorted(current) if full_due else changed
    incremental_statuses = dict(attestation["component_statuses"])
    for name in validated:
        incremental_statuses[name] = _validate_component(current[name])
    full_statuses = {name: _validate_component(value) for name, value in sorted(current.items())}
    incremental_verdict = _snapshot_status(current, incremental_statuses, max_skew)
    full_verdict = _snapshot_status(current, full_statuses, max_skew)
    equivalent = incremental_statuses == full_statuses and incremental_verdict == full_verdict
    if not equivalent:
        raise ValueError("PHASE4CQ_INCREMENTAL_EQUIVALENCE_FAILED")
    proof_payload = {
        "current_snapshot_hash": canonical_hash(current),
        "incremental_statuses": incremental_statuses,
        "full_statuses": full_statuses,
        "incremental_verdict": incremental_verdict,
        "full_verdict": full_verdict,
    }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CQ",
        "input_hash": payload["artifact_hash"],
        "validation_scope": "FULL" if full_due else "INCREMENTAL",
        "changed_components": changed,
        "validated_components": validated,
        "carried_components": sorted(set(current) - set(validated)),
        "component_statuses": incremental_statuses,
        "snapshot_verdict": incremental_verdict,
        "equivalence_proven": True,
        "equivalence_proof_hash": canonical_hash(proof_payload),
        "offline_full_reference_components": len(current),
        "runtime_validator_changed": False,
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
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.evidence.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
