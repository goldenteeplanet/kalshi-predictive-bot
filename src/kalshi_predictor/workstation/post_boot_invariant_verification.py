from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .protected_invariant_recovery_gate import (
    ProtectedInvariantGateResult,
    validate_protected_invariant_gate_result,
)

VERIFICATION_SCHEMA_VERSION = "phase4kg-post-boot-invariant-verification-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class PostBootInvariantVerificationError(ValueError):
    """Stable fail-closed post-boot protected-invariant verification error."""


@dataclass(frozen=True)
class PostBootInvariantDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    restart_intent_hash: str
    database_decision_hash: str
    invariant_gate_hash: str
    invariant_snapshot_hash: str
    decision_hash: str
    read_only: bool = True
    post_boot_invariants_verified: bool = False
    post_boot_chain_may_continue: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    database_write_authorized: bool = False
    execution_authorized: bool = False


def evaluate_post_boot_invariant_verification(
    *,
    restart_intent_hash: str,
    database_decision_hash: str,
    database_verified: bool,
    invariant_gate: Any,
) -> PostBootInvariantDecision:
    _require_hash(restart_intent_hash)
    _require_hash(database_decision_hash)
    if not isinstance(database_verified, bool):
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_FIELD_INVALID")
    if not isinstance(invariant_gate, ProtectedInvariantGateResult):
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_EVIDENCE_TYPE_INVALID")
    try:
        validate_protected_invariant_gate_result(invariant_gate)
    except ValueError as exc:
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_EVIDENCE_INVALID") from exc

    if not database_verified:
        status: VerificationStatus = "INCOMPLETE"
        reasons = ["POST_BOOT_DATABASE_PREREQUISITE_NOT_VERIFIED"]
    elif invariant_gate.status in {"STALE", "INCOMPLETE"}:
        status = "INCOMPLETE"
        reasons = [f"POST_BOOT_{reason}" for reason in invariant_gate.reasons]
    elif not invariant_gate.protected_invariants_proven:
        status = "FAIL"
        invariant_reasons = [r for r in invariant_gate.reasons if r.startswith("INVARIANT_")]
        reasons = [f"POST_BOOT_{reason}" for reason in invariant_reasons] or [
            "POST_BOOT_PROTECTED_INVARIANTS_NOT_PROVEN"
        ]
    else:
        status = "PASS"
        reasons = []

    passed = status == "PASS"
    unsigned = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "restart_intent_hash": restart_intent_hash,
        "database_decision_hash": database_decision_hash,
        "invariant_gate_hash": invariant_gate.gate_hash,
        "invariant_snapshot_hash": invariant_gate.invariant_snapshot_hash,
        "read_only": True,
        "post_boot_invariants_verified": passed,
        "post_boot_chain_may_continue": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "database_write_authorized": False,
        "execution_authorized": False,
    }
    return PostBootInvariantDecision(
        status=status,
        reasons=tuple(reasons),
        restart_intent_hash=restart_intent_hash,
        database_decision_hash=database_decision_hash,
        invariant_gate_hash=invariant_gate.gate_hash,
        invariant_snapshot_hash=invariant_gate.invariant_snapshot_hash,
        decision_hash=_hash(unsigned),
        post_boot_invariants_verified=passed,
        post_boot_chain_may_continue=passed,
    )


def validate_post_boot_invariant_decision(value: Any) -> None:
    if not isinstance(value, PostBootInvariantDecision):
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_DECISION_TYPE_INVALID")
    passed = value.status == "PASS"
    if (
        value.read_only is not True
        or value.post_boot_invariants_verified != passed
        or value.post_boot_chain_may_continue != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.database_write_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_DECISION_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PostBootInvariantVerificationError("POST_BOOT_INVARIANT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
