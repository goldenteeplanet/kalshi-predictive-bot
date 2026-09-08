"""Create and verify exact-chain recovery checkpoints for Phase 4LB ledgers."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from scripts.local.phase4lb_evidence_chain import validate_ledger

SCHEMA = "phase4lc.evidence-ledger-checkpoint.v1"
RECOVERY_SCHEMA = "phase4lc.evidence-ledger-recovery-proof.v1"
POLICY = "EXACT_FULL_CHAIN_ONLY"


def _digest(payload: dict[str, Any], field: str) -> str:
    body = {key: value for key, value in payload.items() if key != field}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def create_checkpoint(receipts: list[object], *, created_at: str) -> dict[str, object]:
    ledger = validate_ledger(receipts)
    if ledger["verdict"] != "PASS" or not receipts:
        raise ValueError("checkpoint requires a non-empty passing ledger")
    created = _timestamp(created_at)
    last = receipts[-1]
    completed = _timestamp(last.get("completed_at")) if isinstance(last, dict) else None
    if created is None or completed is None or created < completed:
        raise ValueError("checkpoint time must be canonical and not precede the tip receipt")
    checkpoint: dict[str, object] = {
        "schema": SCHEMA,
        "recovery_policy": POLICY,
        "created_at": created_at,
        "ledger_sha256": ledger["ledger_sha256"],
        "receipt_count": ledger["receipt_count"],
        "first_phase": ledger["first_phase"],
        "last_phase": ledger["last_phase"],
        "tip_receipt_sha256": ledger["tip_receipt_sha256"],
    }
    checkpoint["checkpoint_sha256"] = _digest(checkpoint, "checkpoint_sha256")
    return checkpoint


def verify_recovery(receipts: object, checkpoint: object) -> dict[str, object]:
    errors: list[str] = []
    ledger = validate_ledger(receipts)
    if ledger["verdict"] != "PASS":
        errors.append("LEDGER_NOT_PASSING")
    if not isinstance(checkpoint, dict):
        checkpoint = {}
        errors.append("CHECKPOINT_NOT_AN_OBJECT")
    if checkpoint.get("schema") != SCHEMA:
        errors.append("CHECKPOINT_BAD_SCHEMA")
    if checkpoint.get("recovery_policy") != POLICY:
        errors.append("UNSAFE_RECOVERY_POLICY")
    if checkpoint.get("checkpoint_sha256") != _digest(checkpoint, "checkpoint_sha256"):
        errors.append("CHECKPOINT_HASH_MISMATCH")
    if _timestamp(checkpoint.get("created_at")) is None:
        errors.append("CHECKPOINT_BAD_TIMESTAMP")
    bindings = {
        "ledger_sha256": ledger.get("ledger_sha256"),
        "receipt_count": ledger.get("receipt_count"),
        "first_phase": ledger.get("first_phase"),
        "last_phase": ledger.get("last_phase"),
        "tip_receipt_sha256": ledger.get("tip_receipt_sha256"),
    }
    for field, expected in bindings.items():
        if checkpoint.get(field) != expected:
            errors.append(f"CHECKPOINT_{field.upper()}_MISMATCH")
    if isinstance(receipts, list) and receipts and isinstance(receipts[-1], dict):
        created = _timestamp(checkpoint.get("created_at"))
        completed = _timestamp(receipts[-1].get("completed_at"))
        if created is not None and completed is not None and created < completed:
            errors.append("CHECKPOINT_PREDATES_TIP")

    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": RECOVERY_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "recovered_ledger_sha256": ledger.get("ledger_sha256"),
        "checkpoint_sha256": checkpoint.get("checkpoint_sha256"),
        "receipt_count": ledger.get("receipt_count"),
        "errors": errors,
        "safety": {
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["recovery_proof_sha256"] = _digest(result, "recovery_proof_sha256")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger")
    parser.add_argument("checkpoint")
    args = parser.parse_args()
    with open(args.ledger, encoding="utf-8") as stream:
        receipts = json.load(stream)
    with open(args.checkpoint, encoding="utf-8") as stream:
        checkpoint = json.load(stream)
    result = verify_recovery(receipts, checkpoint)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
