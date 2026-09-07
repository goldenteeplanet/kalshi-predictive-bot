"""Phase 4BK non-executing emergency abort and recovery playbook evaluator."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bk.emergency-scenario-input.v1"
SCHEMA = "phase4bk.emergency-playbook.v1"
PROOF_SCHEMA = "phase4bk.nonexecuting-recovery-proof.v1"
SCENARIOS = (
    "ABORT_BEFORE_TRANSACTION",
    "ABORT_DURING_VALIDATION",
    "ABORT_DURING_SIMULATION",
    "AMBIGUOUS_COMPLETION",
    "RECEIPT_MISSING",
    "ARTIFACT_PAIR_MISMATCH",
    "DATABASE_IDENTITY_CHANGED",
    "OPERATOR_REVOCATION",
    "SUSPECTED_COMPETING_WRITER",
)
ACTIONS = {
    "ABORT_BEFORE_TRANSACTION": ("REFUSE_BEFORE_MUTATION", "REVALIDATE_FROM_NEW_ARTIFACT_CHAIN"),
    "ABORT_DURING_VALIDATION": ("DISCARD_VALIDATION_OUTPUT", "RESTART_READ_ONLY_VALIDATION"),
    "ABORT_DURING_SIMULATION": ("VERIFY_DISPOSABLE_ROLLBACK", "DISCARD_SIMULATION_ATTEMPT"),
    "AMBIGUOUS_COMPLETION": (
        "MARK_OPERATION_TERMINAL_AMBIGUOUS",
        "REQUIRE_INDEPENDENT_RECONCILIATION",
    ),
    "RECEIPT_MISSING": (
        "MARK_OPERATION_TERMINAL_AMBIGUOUS",
        "RECONSTRUCT_FROM_DISPOSABLE_EVIDENCE",
    ),
    "ARTIFACT_PAIR_MISMATCH": ("REFUSE_ARTIFACT_PAIR", "REQUIRE_FRESH_ATOMIC_PAIR"),
    "DATABASE_IDENTITY_CHANGED": (
        "INVALIDATE_ALL_BOUND_APPROVALS",
        "REQUIRE_NEW_READ_ONLY_IDENTITY",
    ),
    "OPERATOR_REVOCATION": ("REFUSE_ATTEMPT", "REQUIRE_NEW_EXTERNAL_AUTHORIZATION"),
    "SUSPECTED_COMPETING_WRITER": ("REFUSE_ATTEMPT", "WAIT_FOR_INDEPENDENT_READ_ONLY_REVALIDATION"),
}


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BK_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BK_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def build(input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BK_EVALUATION_TIMEZONE_MISSING")
    source = _load(input_path)
    scenarios = source.get("scenarios")
    observed = (
        [item.get("scenario") for item in scenarios if isinstance(item, dict)]
        if isinstance(scenarios, list)
        else []
    )
    if not isinstance(scenarios, list) or observed != list(SCENARIOS):
        raise ValueError("PHASE4BK_SCENARIO_COVERAGE_OR_ORDER_INVALID")
    rows: list[dict[str, Any]] = []
    for sequence, item in enumerate(scenarios, start=1):
        if item.get("sequence") != sequence:
            raise ValueError("PHASE4BK_SCENARIO_SEQUENCE_INVALID")
        evidence_hashes = item.get("evidence_hashes")
        if (
            not isinstance(evidence_hashes, list)
            or not evidence_hashes
            or any(not isinstance(value, str) or len(value) != 64 for value in evidence_hashes)
        ):
            raise ValueError("PHASE4BK_EVIDENCE_HASH_INVALID")
        scenario = item["scenario"]
        row = {
            "sequence": sequence,
            "scenario": scenario,
            "evidence_hashes": sorted(evidence_hashes),
            "declarative_actions": list(ACTIONS[scenario]),
            "terminal_state": "SAFE_REFUSAL_OR_OFFLINE_RECOVERY_REQUIRED",
            "commands_present": False,
            "service_controls_present": False,
            "database_actions_present": False,
            "execution_authorized": False,
        }
        row["row_hash"] = canonical_hash(row)
        rows.append(row)
    evaluated_at = now.astimezone(UTC).isoformat()
    playbook: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BK",
        "evaluated_at": evaluated_at,
        "input_hash": source["artifact_hash"],
        "scenario_count": len(rows),
        "rows": rows,
        "all_scenarios_fail_closed": True,
        "commands_present": False,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    playbook["artifact_hash"] = _hash(playbook)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4BK",
        "evaluated_at": evaluated_at,
        "playbook_hash": playbook["artifact_hash"],
        "kill_commands_present": False,
        "service_controls_present": False,
        "production_database_actions_present": False,
        "production_lock_actions_present": False,
        "exchange_actions_present": False,
        "offline_recovery_only": True,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return playbook, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emergency-scenarios", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--playbook-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    playbook, proof = build(args.emergency_scenarios, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.playbook_output, args.proof_output, playbook, proof)
    print(json.dumps(playbook, sort_keys=True))


if __name__ == "__main__":
    main()
