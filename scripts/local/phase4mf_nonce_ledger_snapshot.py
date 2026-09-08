"""Build and audit inert snapshots and compaction plans for Phase 4ME ledgers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re

from scripts.local.phase4me_nonce_consumption_ledger import validate_ledger

SCHEMA = "phase4mf.nonce-ledger-snapshot.v1"
VALIDATION_SCHEMA = "phase4mf.nonce-ledger-snapshot-validation.v1"
COMPACTION_SCHEMA = "phase4mf.nonce-ledger-compaction-plan.v1"
CHAIN_SCHEMA = "phase4mf.nonce-ledger-snapshot-chain-audit.v1"
POLICY_VERSION = "phase4mf.conservative-terminal-only.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
FIELDS = {
    "schema",
    "source_generation",
    "source_head_sha256",
    "source_validation_sha256",
    "reserved_or_consumed_nonce_sha256",
    "transactions",
    "in_flight_transaction_ids",
    "terminal_transaction_ids",
    "prior_snapshot_sha256",
    "compaction_policy_version",
    "snapshot_sha256",
}
TERMINAL = {"COMMITTED", "ABORTED"}
IN_FLIGHT = {"PREPARED", "CONSUMED"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _body_hash(snapshot: dict[str, object]) -> str:
    return _digest({key: value for key, value in snapshot.items() if key != "snapshot_sha256"})


def make_snapshot(
    ledger_validation: object,
    *,
    prior_snapshot_sha256: str = "0" * 64,
    compaction_policy_version: str = POLICY_VERSION,
) -> dict[str, object]:
    if not isinstance(ledger_validation, dict) or ledger_validation.get("verdict") != "PASS":
        raise ValueError("ledger must have a passing Phase 4ME validation")
    if HEX64.fullmatch(prior_snapshot_sha256) is None:
        raise ValueError("prior snapshot anchor must be a SHA-256 digest")
    if compaction_policy_version != POLICY_VERSION:
        raise ValueError("unsupported compaction policy")
    transactions = ledger_validation["transactions"]
    states = {key: value["state"] for key, value in transactions.items()}
    body: dict[str, object] = {
        "schema": SCHEMA,
        "source_generation": ledger_validation["generation"],
        "source_head_sha256": ledger_validation["head_sha256"],
        "source_validation_sha256": ledger_validation["validation_sha256"],
        "reserved_or_consumed_nonce_sha256": ledger_validation["reserved_or_consumed_nonce_sha256"],
        "transactions": transactions,
        "in_flight_transaction_ids": sorted(
            key for key, state in states.items() if state in IN_FLIGHT
        ),
        "terminal_transaction_ids": sorted(
            key for key, state in states.items() if state in TERMINAL
        ),
        "prior_snapshot_sha256": prior_snapshot_sha256,
        "compaction_policy_version": compaction_policy_version,
    }
    return {**body, "snapshot_sha256": _digest(body)}


def validate_snapshot(
    snapshot: object,
    ledger_validation: object,
    *,
    expected_prior_snapshot_sha256: str,
    expected_policy_version: str = POLICY_VERSION,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
        errors.append("SNAPSHOT_FIELD_SET_INVALID")
        snapshot = {}
    if snapshot.get("schema") != SCHEMA:
        errors.append("SNAPSHOT_SCHEMA_INVALID")
    if snapshot.get("snapshot_sha256") != _body_hash(snapshot):
        errors.append("SNAPSHOT_HASH_INVALID")
    if snapshot.get("prior_snapshot_sha256") != expected_prior_snapshot_sha256:
        errors.append("PRIOR_ANCHOR_MISMATCH")
    if snapshot.get("compaction_policy_version") != expected_policy_version:
        errors.append("POLICY_VERSION_MISMATCH")
    if not isinstance(ledger_validation, dict) or ledger_validation.get("verdict") != "PASS":
        errors.append("SOURCE_LEDGER_NOT_VALIDATED")
        ledger_validation = {}
    bindings = {
        "source_generation": ledger_validation.get("generation"),
        "source_head_sha256": ledger_validation.get("head_sha256"),
        "source_validation_sha256": ledger_validation.get("validation_sha256"),
        "reserved_or_consumed_nonce_sha256": ledger_validation.get(
            "reserved_or_consumed_nonce_sha256"
        ),
        "transactions": ledger_validation.get("transactions"),
    }
    for field, expected in bindings.items():
        if snapshot.get(field) != expected:
            errors.append(f"{field.upper()}_BINDING_MISMATCH")
    transactions = snapshot.get("transactions")
    if not isinstance(transactions, dict):
        errors.append("TRANSACTIONS_INVALID")
        transactions = {}
    states = {
        key: value.get("state")
        for key, value in transactions.items()
        if isinstance(key, str) and isinstance(value, dict)
    }
    if any(state not in TERMINAL | IN_FLIGHT for state in states.values()) or len(states) != len(
        transactions
    ):
        errors.append("TRANSACTION_STATE_INVALID")
    expected_in_flight = sorted(key for key, state in states.items() if state in IN_FLIGHT)
    expected_terminal = sorted(key for key, state in states.items() if state in TERMINAL)
    if snapshot.get("in_flight_transaction_ids") != expected_in_flight:
        errors.append("IN_FLIGHT_INDEX_INVALID")
    if snapshot.get("terminal_transaction_ids") != expected_terminal:
        errors.append("TERMINAL_INDEX_INVALID")
    nonce_set = snapshot.get("reserved_or_consumed_nonce_sha256")
    transaction_nonces = sorted(
        value.get("nonce_sha256") for value in transactions.values() if isinstance(value, dict)
    )
    if not isinstance(nonce_set, list) or nonce_set != sorted(set(transaction_nonces)):
        errors.append("NONCE_SET_INCOMPLETE_OR_INVALID")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": VALIDATION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "snapshot_sha256": snapshot.get("snapshot_sha256"),
        "source_generation": snapshot.get("source_generation"),
        "source_head_sha256": snapshot.get("source_head_sha256"),
        "replay_refusal_nonce_sha256": nonce_set if not errors else [],
        "safety": {
            "read_only": True,
            "snapshot_persistence": False,
            "source_deletion": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["validation_sha256"] = _digest(result)
    return result


def plan_compaction(
    records: object,
    snapshot: object,
    *,
    evaluated_at: str,
    expected_prior_snapshot_sha256: str,
) -> dict[str, object]:
    source = validate_ledger(records, evaluated_at=evaluated_at)
    validation = validate_snapshot(
        snapshot,
        source,
        expected_prior_snapshot_sha256=expected_prior_snapshot_sha256,
    )
    errors = list(validation["errors"])
    delete_through = 0
    retain_reason = "SNAPSHOT_OR_SOURCE_INVALID"
    if not errors:
        in_flight = snapshot["in_flight_transaction_ids"]
        if in_flight:
            retain_reason = "IN_FLIGHT_HISTORY_REQUIRED"
        else:
            delete_through = snapshot["source_generation"]
            retain_reason = "ALL_TRANSACTIONS_TERMINAL"
    result: dict[str, object] = {
        "schema": COMPACTION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "source_generation": source["generation"],
        "source_head_sha256": source["head_sha256"],
        "snapshot_sha256": snapshot.get("snapshot_sha256") if isinstance(snapshot, dict) else None,
        "delete_through_generation": delete_through,
        "retained_from_generation": delete_through + 1,
        "reason": retain_reason,
        "execution_performed": False,
        "replay_refusal_nonce_sha256": validation["replay_refusal_nonce_sha256"],
        "recovery_transactions": snapshot.get("transactions", {})
        if not errors and isinstance(snapshot, dict)
        else {},
    }
    result["plan_sha256"] = _digest(result)
    return result


def audit_snapshot_chain(snapshots: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(snapshots, list):
        errors.append("SNAPSHOTS_NOT_LIST")
        snapshots = []
    prior = "0" * 64
    prior_generation = -1
    generation_hashes: dict[int, str] = {}
    for index, snapshot in enumerate(snapshots):
        row_errors: list[str] = []
        if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
            row_errors.append("FIELD_SET_INVALID")
            snapshot = {}
        if snapshot.get("snapshot_sha256") != _body_hash(snapshot):
            row_errors.append("HASH_INVALID")
        if snapshot.get("prior_snapshot_sha256") != prior:
            row_errors.append("ANCHOR_INVALID")
        generation = snapshot.get("source_generation")
        if type(generation) is not int or generation < prior_generation:
            row_errors.append("GENERATION_ROLLBACK")
        elif generation in generation_hashes and generation_hashes[generation] != snapshot.get(
            "snapshot_sha256"
        ):
            row_errors.append("DIVERGENT_SAME_GENERATION")
        if row_errors:
            errors.extend(f"SNAPSHOT_{index}_{error}" for error in sorted(set(row_errors)))
            break
        generation_hashes[generation] = str(snapshot.get("snapshot_sha256"))
        prior_generation = generation
        prior = str(snapshot.get("snapshot_sha256"))
    result: dict[str, object] = {
        "schema": CHAIN_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "snapshot_count": len(snapshots),
        "head_snapshot_sha256": prior,
        "highest_generation": prior_generation,
        "read_only": True,
    }
    result["audit_sha256"] = _digest(result)
    return result


def nonce_replay_verdict(snapshot_validation: object, nonce_sha256: str) -> str:
    if not isinstance(snapshot_validation, dict) or snapshot_validation.get("verdict") != "PASS":
        return "REFUSE_SNAPSHOT_UNTRUSTED"
    if nonce_sha256 in snapshot_validation.get("replay_refusal_nonce_sha256", []):
        return "REFUSE_NONCE_RESERVED_OR_CONSUMED"
    return "NOT_FOUND_REQUIRES_AUTHORITATIVE_LEDGER_CHECK"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger")
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--prior-snapshot", default="0" * 64)
    args = parser.parse_args()
    with open(args.ledger, encoding="utf-8") as stream:
        records = json.load(stream)
    source = validate_ledger(records, evaluated_at=args.evaluated_at)
    snapshot = make_snapshot(source, prior_snapshot_sha256=args.prior_snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
