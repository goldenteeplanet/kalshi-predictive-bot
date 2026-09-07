"""Certify market-data latency improvements without invariant regressions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ct.certification-input.v1"
REPORT_SCHEMA = "phase4ct.market-data-certification.v1"
REQUIRED_PHASES = tuple(f"4C{suffix}" for suffix in "ABCDEFGHIJKLMNOPQRS")
INVARIANTS = ("freshness", "coherence", "provenance", "safety")


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {
            key: value
            for key, value in payload.items()
            if key not in {"artifact_hash", "evidence_hash"}
        }
    return canonical_hash(payload)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "phase_evidence", "artifact_hash"}:
        raise ValueError("PHASE4CT_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CT_INPUT_SCHEMA_OR_HASH_INVALID")
    evidence = payload.get("phase_evidence")
    if not isinstance(evidence, list) or len(evidence) != len(REQUIRED_PHASES):
        raise ValueError("PHASE4CT_EVIDENCE_COUNT_INVALID")
    by_phase: dict[str, dict[str, Any]] = {}
    required_fields = {
        "phase",
        "focused_tests_passed",
        "cumulative_tests_passed",
        "latency_before_us",
        "latency_after_us",
        *INVARIANTS,
        "evidence_hash",
    }
    for row in evidence:
        if not isinstance(row, dict) or set(row) != required_fields:
            raise ValueError("PHASE4CT_EVIDENCE_FIELDS_INVALID")
        phase = row["phase"]
        if phase not in REQUIRED_PHASES or phase in by_phase:
            raise ValueError("PHASE4CT_PHASE_SET_INVALID")
        if row["evidence_hash"] != _hash(row):
            raise ValueError("PHASE4CT_EVIDENCE_HASH_INVALID")
        for field in ("focused_tests_passed", "cumulative_tests_passed"):
            if not isinstance(row[field], bool):
                raise ValueError("PHASE4CT_TEST_STATUS_INVALID")
        for field in ("latency_before_us", "latency_after_us"):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("PHASE4CT_LATENCY_INVALID")
        if any(row[field] not in {"PASS", "NOT_APPLICABLE"} for field in INVARIANTS):
            raise ValueError("PHASE4CT_INVARIANT_VALUE_INVALID")
        by_phase[phase] = row
    if set(by_phase) != set(REQUIRED_PHASES):
        raise ValueError("PHASE4CT_PHASE_SET_INVALID")

    decisions = []
    for phase in REQUIRED_PHASES:
        row = by_phase[phase]
        reasons = []
        if not row["focused_tests_passed"] or not row["cumulative_tests_passed"]:
            reasons.append("TEST_GATE_FAILED")
        if any(row[field] != "PASS" for field in INVARIANTS):
            reasons.append("INVARIANT_NOT_PROVEN")
        if row["latency_after_us"] > row["latency_before_us"]:
            reasons.append("LATENCY_REGRESSION")
        decisions.append(
            {
                "phase": phase,
                "status": "PASS" if not reasons else "FAIL",
                "latency_delta_us": row["latency_after_us"] - row["latency_before_us"],
                "reasons": reasons,
                "evidence_hash": row["evidence_hash"],
            }
        )
    total_before = sum(row["latency_before_us"] for row in by_phase.values())
    total_after = sum(row["latency_after_us"] for row in by_phase.values())
    strict_improvements = sum(
        row["latency_after_us"] < row["latency_before_us"] for row in by_phase.values()
    )
    certified = (
        all(row["status"] == "PASS" for row in decisions)
        and strict_improvements > 0
        and total_after < total_before
    )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CT",
        "input_hash": payload["artifact_hash"],
        "status": "MARKET_DATA_WORKSTREAM_CERTIFIED" if certified else "REFUSE",
        "certified": certified,
        "phase_decisions": decisions,
        "total_latency_before_us": total_before,
        "total_latency_after_us": total_after,
        "total_latency_reduction_us": total_before - total_after,
        "strict_improvement_count": strict_improvements,
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
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.evidence.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
