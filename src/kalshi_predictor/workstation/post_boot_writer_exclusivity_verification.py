from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .writer_exclusivity_recovery_gate import (
    WriterExclusivityGateResult,
    validate_writer_exclusivity_gate_result,
)

VERIFICATION_SCHEMA_VERSION = "phase4kh-post-boot-writer-exclusivity-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE"]


class PostBootWriterExclusivityError(ValueError):
    """Stable fail-closed post-boot writer-exclusivity error."""


@dataclass(frozen=True)
class PostBootWriterExclusivityDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    restart_intent_hash: str
    invariant_decision_hash: str
    writer_gate_hash: str
    writer_identities_hash: str
    writer_count: int
    decision_hash: str
    read_only: bool = True
    post_boot_writer_exclusivity_verified: bool = False
    post_boot_chain_may_continue: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def evaluate_post_boot_writer_exclusivity(
    *,
    restart_intent_hash: str,
    invariant_decision_hash: str,
    invariants_verified: bool,
    writer_gate: Any,
) -> PostBootWriterExclusivityDecision:
    _require_hash(restart_intent_hash)
    _require_hash(invariant_decision_hash)
    if not isinstance(invariants_verified, bool):
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_FIELD_INVALID")
    if not isinstance(writer_gate, WriterExclusivityGateResult):
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_EVIDENCE_TYPE_INVALID")
    try:
        validate_writer_exclusivity_gate_result(writer_gate)
    except ValueError as exc:
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_EVIDENCE_INVALID") from exc

    if not invariants_verified:
        status: VerificationStatus = "INCOMPLETE"
        reasons = ["POST_BOOT_INVARIANT_PREREQUISITE_NOT_VERIFIED"]
    elif writer_gate.status in {"STALE", "INCOMPLETE"}:
        status = "INCOMPLETE"
        reasons = [f"POST_BOOT_{reason}" for reason in writer_gate.reasons]
    elif writer_gate.status != "PASSED" or not writer_gate.writer_exclusivity_proven:
        status = "FAIL"
        reasons = [f"POST_BOOT_{reason}" for reason in writer_gate.reasons] or [
            "POST_BOOT_WRITER_EXCLUSIVITY_NOT_PROVEN"
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
        "invariant_decision_hash": invariant_decision_hash,
        "writer_gate_hash": writer_gate.gate_hash,
        "writer_identities_hash": writer_gate.writer_identities_hash,
        "writer_count": writer_gate.writer_count,
        "read_only": True,
        "post_boot_writer_exclusivity_verified": passed,
        "post_boot_chain_may_continue": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return PostBootWriterExclusivityDecision(
        status=status,
        reasons=tuple(reasons),
        restart_intent_hash=restart_intent_hash,
        invariant_decision_hash=invariant_decision_hash,
        writer_gate_hash=writer_gate.gate_hash,
        writer_identities_hash=writer_gate.writer_identities_hash,
        writer_count=writer_gate.writer_count,
        decision_hash=_hash(unsigned),
        post_boot_writer_exclusivity_verified=passed,
        post_boot_chain_may_continue=passed,
    )


def validate_post_boot_writer_exclusivity_decision(value: Any) -> None:
    if not isinstance(value, PostBootWriterExclusivityDecision):
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_DECISION_TYPE_INVALID")
    passed = value.status == "PASS"
    if (
        value.read_only is not True
        or value.post_boot_writer_exclusivity_verified != passed
        or value.post_boot_chain_may_continue != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_DECISION_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PostBootWriterExclusivityError("POST_BOOT_WRITER_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
