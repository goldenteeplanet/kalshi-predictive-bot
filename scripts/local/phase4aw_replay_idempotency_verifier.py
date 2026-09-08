"""Phase 4AW artifact-only replay and idempotency verifier."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

HISTORY_SCHEMA = "phase4aw.attempt-history.v1"
SCHEMA = "phase4aw.replay-idempotency-verdict.v1"
PROOF_SCHEMA = "phase4aw.no-second-mutation-proof.v1"
OUTCOMES = {"SUCCESS", "ROLLED_BACK", "TIMEOUT", "AMBIGUOUS"}


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AW_HISTORY_UNREADABLE") from exc
    if payload.get("schema") != HISTORY_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AW_HISTORY_SCHEMA_OR_HASH_INVALID")
    return payload


def build(history_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AW_EVALUATION_TIMEZONE_MISSING")
    history = _load(history_path)
    events = history.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("PHASE4AW_EVENTS_MISSING")
    seen_attempts: set[str] = set()
    seen_receipts: set[str] = set()
    terminal_operations: set[str] = set()
    active_generation: dict[str, int] = {}
    current_state: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for expected_sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or event.get("sequence") != expected_sequence:
            raise ValueError("PHASE4AW_SEQUENCE_INVALID")
        required = (
            "attempt_id",
            "operation_hash",
            "ticker",
            "envelope_hash",
            "envelope_generation",
            "approval_hash",
            "database_state_before_hash",
            "database_state_after_hash",
            "outcome",
        )
        if any(not isinstance(event.get(field), str | int) for field in required):
            raise ValueError("PHASE4AW_EVENT_FIELDS_INVALID")
        attempt, operation, ticker = (
            str(event["attempt_id"]),
            str(event["operation_hash"]),
            str(event["ticker"]),
        )
        outcome = str(event["outcome"])
        if not attempt or outcome not in OUTCOMES:
            raise ValueError("PHASE4AW_EVENT_OUTCOME_OR_ATTEMPT_INVALID")
        reasons: list[str] = []
        if attempt in seen_attempts:
            reasons.append("DUPLICATE_ATTEMPT_ID")
        seen_attempts.add(attempt)
        receipt = event.get("receipt_hash")
        if receipt is not None:
            if not isinstance(receipt, str) or len(receipt) != 64:
                reasons.append("RECEIPT_HASH_INVALID")
            elif receipt in seen_receipts:
                reasons.append("RECEIPT_REUSE")
            else:
                seen_receipts.add(receipt)
        generation = event["envelope_generation"]
        if not isinstance(generation, int) or generation < 1:
            reasons.append("ENVELOPE_GENERATION_INVALID")
        elif generation < active_generation.get(ticker, generation):
            reasons.append("SUPERSEDED_ENVELOPE")
        else:
            active_generation[ticker] = generation
        known_state = current_state.get(ticker)
        if known_state is not None and event["database_state_before_hash"] != known_state:
            reasons.append("DATABASE_STATE_DISCONTINUITY")
        if operation in terminal_operations:
            reasons.append("OPERATION_ALREADY_TERMINAL")
        mutating_success = outcome == "SUCCESS"
        if outcome in {"ROLLED_BACK", "TIMEOUT"} and (
            event["database_state_before_hash"] != event["database_state_after_hash"]
        ):
            reasons.append("NONMUTATING_OUTCOME_CHANGED_STATE")
        if (
            mutating_success
            and event["database_state_before_hash"] == event["database_state_after_hash"]
        ):
            reasons.append("SUCCESS_WITHOUT_STATE_CHANGE")
        if mutating_success and receipt is None:
            reasons.append("SUCCESS_RECEIPT_MISSING")
        if outcome == "AMBIGUOUS" or (mutating_success and "SUCCESS_RECEIPT_MISSING" in reasons):
            terminal_operations.add(operation)
        elif mutating_success and not reasons:
            terminal_operations.add(operation)
        if not reasons:
            current_state[ticker] = str(event["database_state_after_hash"])
        decision = "REFUSED" if reasons else "RECORDED"
        row = {
            "sequence": expected_sequence,
            "attempt_id_hash": canonical_hash(attempt),
            "operation_hash": operation,
            "outcome": outcome,
            "decision": decision,
            "reason_codes": sorted(reasons),
            "may_mutate": mutating_success and decision == "RECORDED",
            "second_mutation_possible": False,
            "execution_authorized": False,
        }
        row["row_hash"] = canonical_hash(row)
        rows.append(row)
    successful_operations = [row["operation_hash"] for row in rows if row["may_mutate"]]
    if len(successful_operations) != len(set(successful_operations)):
        raise ValueError("PHASE4AW_SECOND_MUTATION_DETECTED")
    evaluated_at = now.astimezone(UTC).isoformat()
    verdict: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AW",
        "evaluated_at": evaluated_at,
        "history_hash": history["artifact_hash"],
        "event_count": len(rows),
        "rows": rows,
        "refusal_count": sum(row["decision"] == "REFUSED" for row in rows),
        "no_replay_can_cause_second_mutation": True,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    verdict["artifact_hash"] = _hash(verdict)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4AW",
        "evaluated_at": evaluated_at,
        "verdict_hash": verdict["artifact_hash"],
        "terminal_operation_hashes": sorted(terminal_operations),
        "attempt_id_set_hash": canonical_hash(sorted(seen_attempts)),
        "receipt_set_hash": canonical_hash(sorted(seen_receipts)),
        "second_mutation_count": 0,
        "production_database_mutated": False,
        "research_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return verdict, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-history", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--verdict-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    verdict, proof = build(args.attempt_history, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.verdict_output, args.proof_output, verdict, proof)
    print(json.dumps(verdict, sort_keys=True))


if __name__ == "__main__":
    main()
