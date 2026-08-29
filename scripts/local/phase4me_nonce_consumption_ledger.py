"""Pure hash-linked model for crash-consistent authorization nonce consumption."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

SCHEMA = "phase4me.nonce-consumption-ledger-validation.v1"
RECORD_SCHEMA = "phase4me.nonce-consumption-record.v1"
RECOVERY_SCHEMA = "phase4me.nonce-consumption-recovery-plan.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
OPERATIONS = ("PREPARE", "DURABLE_CONSUME", "COMMIT", "ABORT")
FIELDS = {
    "schema",
    "record_id",
    "operation",
    "generation",
    "expected_generation",
    "transaction_id",
    "token_id",
    "nonce_sha256",
    "receipt_sha256",
    "occurred_at",
    "previous_record_sha256",
    "record_sha256",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def make_record(
    operation: str,
    *,
    generation: int,
    expected_generation: int,
    transaction_id: str,
    token_id: str,
    nonce_sha256: str,
    receipt_sha256: str | None,
    occurred_at: str,
    previous_record_sha256: str,
) -> dict[str, object]:
    identity = {
        "operation": operation,
        "transaction_id": transaction_id,
        "generation": generation,
        "token_id": token_id,
        "nonce_sha256": nonce_sha256,
    }
    body: dict[str, object] = {
        "schema": RECORD_SCHEMA,
        "record_id": _digest(identity),
        "operation": operation,
        "generation": generation,
        "expected_generation": expected_generation,
        "transaction_id": transaction_id,
        "token_id": token_id,
        "nonce_sha256": nonce_sha256,
        "receipt_sha256": receipt_sha256,
        "occurred_at": occurred_at,
        "previous_record_sha256": previous_record_sha256,
    }
    return {**body, "record_sha256": _digest(body)}


def validate_ledger(
    records: object,
    *,
    evaluated_at: str,
    expected_generation: int | None = None,
    expected_head_sha256: str | None = None,
) -> dict[str, object]:
    source_sha256 = _digest(records)
    errors: list[str] = []
    if not isinstance(records, list):
        errors.append("LEDGER_NOT_LIST")
        records = []
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    generation = 0
    head = "0" * 64
    previous_time: datetime | None = None
    identities: dict[str, str] = {}
    transactions: dict[str, dict[str, object]] = {}
    nonce_owners: dict[str, str] = {}
    accepted: list[dict[str, object]] = []
    for source_index, candidate in enumerate(records):
        row_errors: list[str] = []
        if not isinstance(candidate, dict) or set(candidate) != FIELDS:
            row_errors.append("FIELD_SET_INVALID")
            candidate = {}
        record_id = candidate.get("record_id")
        fingerprint = _digest(candidate)
        if HEX64.fullmatch(str(record_id)) is None:
            row_errors.append("RECORD_ID_INVALID")
        elif record_id in identities:
            if identities[record_id] == fingerprint:
                continue
            row_errors.append("CONFLICTING_REPLAY")
        else:
            identities[str(record_id)] = fingerprint
        body = {key: value for key, value in candidate.items() if key != "record_sha256"}
        if candidate.get("record_sha256") != _digest(body):
            row_errors.append("RECORD_HASH_INVALID")
        operation = candidate.get("operation")
        if operation not in OPERATIONS:
            row_errors.append("OPERATION_INVALID")
        if candidate.get("expected_generation") != generation:
            row_errors.append("CAS_GENERATION_MISMATCH")
        if candidate.get("generation") != generation + 1:
            row_errors.append("GENERATION_SEQUENCE_INVALID")
        if candidate.get("previous_record_sha256") != head:
            row_errors.append("CHAIN_LINK_INVALID")
        occurred = _time(candidate.get("occurred_at"))
        if occurred is None:
            row_errors.append("TIME_INVALID")
        elif now is not None and occurred > now:
            row_errors.append("RECORD_FROM_FUTURE")
        elif previous_time is not None and occurred < previous_time:
            row_errors.append("TIME_REVERSED")
        for field in ("token_id", "nonce_sha256"):
            if HEX64.fullmatch(str(candidate.get(field))) is None:
                row_errors.append(f"{field.upper()}_INVALID")
        receipt = candidate.get("receipt_sha256")
        if operation in {"DURABLE_CONSUME", "COMMIT"}:
            if HEX64.fullmatch(str(receipt)) is None:
                row_errors.append("RECEIPT_HASH_REQUIRED")
        elif receipt is not None:
            row_errors.append("RECEIPT_HASH_FORBIDDEN")
        transaction_id = candidate.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            row_errors.append("TRANSACTION_ID_INVALID")
        transaction = transactions.get(str(transaction_id))
        nonce = str(candidate.get("nonce_sha256"))
        token = candidate.get("token_id")
        if operation == "PREPARE":
            if transaction is not None:
                row_errors.append("TRANSACTION_ALREADY_EXISTS")
            if nonce in nonce_owners:
                row_errors.append("NONCE_ALREADY_RESERVED_OR_CONSUMED")
        elif transaction is None:
            row_errors.append("PREPARE_MISSING")
        else:
            assert transaction is not None
            if transaction["token_id"] != token or transaction["nonce_sha256"] != nonce:
                row_errors.append("TOKEN_OR_NONCE_SUBSTITUTION")
            state = transaction["state"]
            if operation == "DURABLE_CONSUME":
                if state != "PREPARED":
                    row_errors.append("CONSUME_TRANSITION_INVALID")
            elif operation == "COMMIT":
                if state != "CONSUMED":
                    row_errors.append("COMMIT_TRANSITION_INVALID")
                elif transaction.get("receipt_sha256") != receipt:
                    row_errors.append("RECEIPT_SUBSTITUTION")
            elif operation == "ABORT" and state != "PREPARED":
                row_errors.append("ABORT_TRANSITION_INVALID")
        if row_errors:
            errors.extend(f"RECORD_{source_index}_{error}" for error in sorted(set(row_errors)))
            break
        if operation == "PREPARE":
            transactions[str(transaction_id)] = {
                "state": "PREPARED",
                "token_id": token,
                "nonce_sha256": nonce,
                "receipt_sha256": None,
            }
            nonce_owners[nonce] = str(transaction_id)
        elif operation == "DURABLE_CONSUME":
            transactions[str(transaction_id)]["state"] = "CONSUMED"
            transactions[str(transaction_id)]["receipt_sha256"] = receipt
        elif operation == "COMMIT":
            transactions[str(transaction_id)]["state"] = "COMMITTED"
        elif operation == "ABORT":
            transactions[str(transaction_id)]["state"] = "ABORTED"
        accepted.append(candidate)
        generation += 1
        head = str(candidate.get("record_sha256"))
        previous_time = occurred
    if expected_generation is not None and generation != expected_generation:
        errors.append("ANCHORED_GENERATION_MISMATCH")
    if expected_head_sha256 is not None and head != expected_head_sha256:
        errors.append("ANCHORED_HEAD_MISMATCH")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "source_sha256": source_sha256,
        "source_unchanged": _digest(records) == source_sha256,
        "accepted_record_count": len(accepted),
        "generation": generation,
        "head_sha256": head,
        "reserved_or_consumed_nonce_sha256": sorted(nonce_owners),
        "transactions": {key: transactions[key] for key in sorted(transactions)},
        "safety": {
            "simulation_only": True,
            "ledger_write": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["validation_sha256"] = _digest(result)
    return result


def plan_recovery(validation: object, *, occurred_at: str) -> dict[str, object]:
    errors: list[str] = []
    actions: list[dict[str, object]] = []
    if not isinstance(validation, dict) or validation.get("verdict") != "PASS":
        errors.append("LEDGER_NOT_VALIDATED")
    elif _time(occurred_at) is None:
        errors.append("RECOVERY_TIME_INVALID")
    else:
        generation = validation["generation"]
        previous = validation["head_sha256"]
        for transaction_id, transaction in validation["transactions"].items():
            operation = None
            receipt = None
            if transaction["state"] == "PREPARED":
                operation = "ABORT"
            elif transaction["state"] == "CONSUMED":
                operation = "COMMIT"
                receipt = transaction["receipt_sha256"]
            if operation is None:
                continue
            actions.append(
                {
                    "action": "PROPOSE_LEDGER_APPEND",
                    "record": make_record(
                        operation,
                        generation=generation + 1,
                        expected_generation=generation,
                        transaction_id=transaction_id,
                        token_id=transaction["token_id"],
                        nonce_sha256=transaction["nonce_sha256"],
                        receipt_sha256=receipt,
                        occurred_at=occurred_at,
                        previous_record_sha256=previous,
                    ),
                    "persistence_performed": False,
                }
            )
            generation += 1
            previous = str(actions[-1]["record"]["record_sha256"])
    result: dict[str, object] = {
        "schema": RECOVERY_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "actions": actions,
        "simulation_only": True,
    }
    result["recovery_plan_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger")
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--expected-generation", type=int)
    parser.add_argument("--expected-head")
    args = parser.parse_args()
    with open(args.ledger, encoding="utf-8") as stream:
        records = json.load(stream)
    result = validate_ledger(
        records,
        evaluated_at=args.evaluated_at,
        expected_generation=args.expected_generation,
        expected_head_sha256=args.expected_head,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
