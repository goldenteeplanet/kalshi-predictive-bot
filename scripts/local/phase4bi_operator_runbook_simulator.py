"""Phase 4BI deterministic artifact-only operator runbook simulator."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bi.runbook-input.v1"
SCHEMA = "phase4bi.operator-runbook-transcript.v1"
PROOF_SCHEMA = "phase4bi.workflow-safety-proof.v1"
STAGES = (
    "ARTIFACT_COLLECTION",
    "INDEPENDENT_VERIFICATION",
    "HUMAN_REVIEW",
    "READINESS_COMPILATION",
    "AUTHORIZATION_VALIDATION",
    "DISPOSABLE_SIMULATION",
    "ROLLBACK_CONFIRMATION",
    "FINAL_REFUSAL_OR_HANDOFF",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BI_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BI_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def build(input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BI_EVALUATION_TIMEZONE_MISSING")
    source = _load(input_path)
    steps = source.get("steps")
    if not isinstance(steps, list) or [
        step.get("stage") for step in steps if isinstance(step, dict)
    ] != list(STAGES):
        raise ValueError("PHASE4BI_STAGE_COVERAGE_OR_ORDER_INVALID")
    rows: list[dict[str, Any]] = []
    blocked = False
    previous_hash = "0" * 64
    rollback_verified = False
    authorization_valid = False
    for sequence, step in enumerate(steps, start=1):
        if step.get("sequence") != sequence:
            raise ValueError("PHASE4BI_SEQUENCE_INVALID")
        status = step.get("status")
        if status not in {"PASS", "REFUSED", "SKIPPED", "HANDOFF_NON_PRODUCTION", "FINAL_REFUSAL"}:
            raise ValueError("PHASE4BI_STATUS_INVALID")
        if step.get("input_hash") != previous_hash:
            raise ValueError("PHASE4BI_STEP_LINEAGE_INVALID")
        stage = step["stage"]
        reasons = step.get("reason_codes")
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            raise ValueError("PHASE4BI_REASON_CODES_INVALID")
        if stage != "FINAL_REFUSAL_OR_HANDOFF":
            if blocked:
                if status != "SKIPPED":
                    raise ValueError("PHASE4BI_UNSAFE_CONTINUATION_AFTER_REFUSAL")
            else:
                if status == "SKIPPED":
                    raise ValueError("PHASE4BI_UNSAFE_STAGE_BYPASS")
                if status == "REFUSED":
                    blocked = True
                elif status != "PASS":
                    raise ValueError("PHASE4BI_INTERMEDIATE_STATUS_INVALID")
        if stage == "AUTHORIZATION_VALIDATION" and status == "PASS":
            authorization_valid = True
        if stage == "DISPOSABLE_SIMULATION" and status == "PASS" and not authorization_valid:
            raise ValueError("PHASE4BI_SIMULATION_WITHOUT_AUTHORIZATION")
        if stage == "ROLLBACK_CONFIRMATION" and status == "PASS":
            rollback_verified = True
        if stage == "FINAL_REFUSAL_OR_HANDOFF":
            if blocked and status != "FINAL_REFUSAL":
                raise ValueError("PHASE4BI_REFUSAL_NOT_TERMINAL")
            if not blocked and status != "HANDOFF_NON_PRODUCTION":
                raise ValueError("PHASE4BI_VALID_FLOW_MISSING_HANDOFF")
            if status == "HANDOFF_NON_PRODUCTION" and not rollback_verified:
                raise ValueError("PHASE4BI_HANDOFF_WITHOUT_ROLLBACK")
        row = {
            "sequence": sequence,
            "stage": stage,
            "status": status,
            "input_hash": previous_hash,
            "reason_codes": sorted(set(reasons)),
            "production_mutation_performed": False,
            "execution_authorized": False,
        }
        row["step_hash"] = canonical_hash(row)
        rows.append(row)
        previous_hash = row["step_hash"]
    final_status = rows[-1]["status"]
    evaluated_at = now.astimezone(UTC).isoformat()
    transcript: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BI",
        "evaluated_at": evaluated_at,
        "input_hash": source["artifact_hash"],
        "steps": rows,
        "step_count": len(rows),
        "final_status": final_status,
        "unsafe_sequence_refused": True,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    transcript["artifact_hash"] = _hash(transcript)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4BI",
        "evaluated_at": evaluated_at,
        "transcript_hash": transcript["artifact_hash"],
        "exact_stage_order_verified": True,
        "authorization_before_simulation_verified": True,
        "rollback_before_handoff_verified": final_status == "FINAL_REFUSAL" or rollback_verified,
        "handoff_is_non_production": final_status == "HANDOFF_NON_PRODUCTION",
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return transcript, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runbook-input", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--transcript-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    transcript, proof = build(args.runbook_input, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.transcript_output, args.proof_output, transcript, proof)
    print(json.dumps(transcript, sort_keys=True))


if __name__ == "__main__":
    main()
