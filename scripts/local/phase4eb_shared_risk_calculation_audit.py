"""Audit calculations for safe reuse across independent Phase 3M/3N decisions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4eb.audit-input.v1"
REPORT_SCHEMA = "phase4eb.audit-report.v1"
CONSUMERS = {"POSITION_SIZING", "ADVANCED_RISK"}


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
    if set(payload) != {"schema", "calculations", "artifact_hash"}:
        raise ValueError("PHASE4EB_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EB_INPUT_SCHEMA_OR_HASH_INVALID")
    calculations = payload.get("calculations")
    if not isinstance(calculations, list) or not calculations:
        raise ValueError("PHASE4EB_CALCULATIONS_EMPTY")
    seen = set()
    decisions = []
    fields = {
        "calculation_id",
        "consumers",
        "input_lineage_hash",
        "semantics_hash",
        "output_schema_hash",
        "exact_decimal",
        "decision_independent",
        "mutable_state",
        "consumer_output_coupling",
    }
    for row in calculations:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("PHASE4EB_CALCULATION_FIELDS_INVALID")
        identifier = row["calculation_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EB_CALCULATION_ID_INVALID")
        seen.add(identifier)
        consumers = row["consumers"]
        if (
            not isinstance(consumers, list)
            or not consumers
            or len(consumers) != len(set(consumers))
            or any(item not in CONSUMERS for item in consumers)
        ):
            raise ValueError("PHASE4EB_CONSUMERS_INVALID")
        if not all(
            _digest(row[field])
            for field in ("input_lineage_hash", "semantics_hash", "output_schema_hash")
        ):
            raise ValueError("PHASE4EB_HASH_INVALID")
        for field in (
            "exact_decimal",
            "decision_independent",
            "mutable_state",
            "consumer_output_coupling",
        ):
            if not isinstance(row[field], bool):
                raise ValueError("PHASE4EB_BOOLEAN_INVALID")
        reasons = []
        if set(consumers) != CONSUMERS:
            reasons.append("NOT_SHARED_BY_BOTH_DECISIONS")
        if not row["exact_decimal"]:
            reasons.append("NON_EXACT_ARITHMETIC")
        if not row["decision_independent"]:
            reasons.append("DECISION_SPECIFIC_SEMANTICS")
        if row["mutable_state"]:
            reasons.append("MUTABLE_STATE_DEPENDENCY")
        if row["consumer_output_coupling"]:
            reasons.append("CONSUMER_OUTPUT_COUPLING")
        decisions.append(
            {
                "calculation_id": identifier,
                "status": "SAFE_TO_REUSE" if not reasons else "KEEP_INDEPENDENT",
                "consumers": sorted(consumers),
                "input_lineage_hash": row["input_lineage_hash"],
                "semantics_hash": row["semantics_hash"],
                "output_schema_hash": row["output_schema_hash"],
                "reasons": reasons,
            }
        )
    decisions.sort(key=lambda row: row["calculation_id"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EB",
        "input_hash": payload["artifact_hash"],
        "decisions": decisions,
        "safe_reuse_ids": [
            row["calculation_id"] for row in decisions if row["status"] == "SAFE_TO_REUSE"
        ],
        "independent_ids": [
            row["calculation_id"] for row in decisions if row["status"] == "KEEP_INDEPENDENT"
        ],
        "decision_outputs_shared": False,
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
    parser.add_argument("--calculations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.calculations.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
