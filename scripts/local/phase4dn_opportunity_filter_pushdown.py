"""Audit provably safe opportunity-filter pushdown on synthetic candidates."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dn.filter-input.v1"
REPORT_SCHEMA = "phase4dn.filter-report.v1"
MAX_CANDIDATES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DN_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DN_DECIMAL_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DN_DECIMAL_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "minimum_score", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4DN_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DN_INPUT_SCHEMA_OR_HASH_INVALID")
    minimum = _decimal(payload["minimum_score"])
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4DN_CANDIDATE_COUNT_INVALID")
    fields = {
        "candidate_id",
        "market_open",
        "evidence_fresh",
        "hard_blocked",
        "max_possible_score",
        "full_score",
    }
    identifiers: set[str] = set()
    decisions = []
    baseline_eligible = []
    pushdown_survivors = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != fields:
            raise ValueError("PHASE4DN_CANDIDATE_FIELDS_INVALID")
        identifier = candidate["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DN_CANDIDATE_ID_INVALID")
        identifiers.add(identifier)
        if any(
            not isinstance(candidate[field], bool)
            for field in ("market_open", "evidence_fresh", "hard_blocked")
        ):
            raise ValueError("PHASE4DN_BOOLEAN_POLICY_INVALID")
        upper = _decimal(candidate["max_possible_score"])
        full = _decimal(candidate["full_score"])
        if full > upper:
            raise ValueError("PHASE4DN_UPPER_BOUND_VIOLATED")
        baseline = (
            candidate["market_open"]
            and candidate["evidence_fresh"]
            and not candidate["hard_blocked"]
            and full >= minimum
        )
        if baseline:
            baseline_eligible.append(identifier)
        reasons = []
        if not candidate["market_open"]:
            reasons.append("MARKET_CLOSED")
        if not candidate["evidence_fresh"]:
            reasons.append("EVIDENCE_STALE")
        if candidate["hard_blocked"]:
            reasons.append("HARD_BLOCKED")
        if upper < minimum:
            reasons.append("PROVABLE_SCORE_UPPER_BOUND_BELOW_MINIMUM")
        survives = not reasons
        if survives:
            pushdown_survivors.append(identifier)
        decisions.append(
            {
                "candidate_id": identifier,
                "pushdown_disposition": "KEEP_FOR_FULL_RANKING" if survives else "REJECT_EARLY",
                "reasons": reasons,
                "baseline_eligible": baseline,
            }
        )
    false_rejections = sorted(set(baseline_eligible) - set(pushdown_survivors))
    if false_rejections:
        raise ValueError("PHASE4DN_FALSE_REJECTION_DETECTED")
    rejected = [
        decision["candidate_id"]
        for decision in decisions
        if decision["pushdown_disposition"] == "REJECT_EARLY"
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DN",
        "input_hash": payload["artifact_hash"],
        "candidate_decisions": decisions,
        "baseline_eligible_ids": sorted(baseline_eligible),
        "pushdown_survivor_ids": sorted(pushdown_survivors),
        "early_rejected_ids": sorted(rejected),
        "false_rejection_ids": false_rejections,
        "expensive_computations_avoided": len(rejected),
        "full_eligibility_preserved": True,
        "ranking_records_created": 0,
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
