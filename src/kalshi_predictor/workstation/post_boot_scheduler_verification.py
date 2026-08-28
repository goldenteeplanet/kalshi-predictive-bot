from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .authoritative_scheduler_health import (
    SchedulerHealthEvidence,
    validate_scheduler_health_evidence,
)

VERIFICATION_SCHEMA_VERSION = "phase4ke-post-boot-scheduler-verification-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class PostBootSchedulerVerificationError(ValueError):
    """Stable fail-closed post-boot scheduler verification error."""


@dataclass(frozen=True)
class PostBootSchedulerDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    restart_intent_hash: str
    post_boot_wsl_decision_hash: str
    scheduler_evidence_hash: str
    decision_hash: str
    read_only: bool = True
    post_boot_scheduler_verified: bool = False
    post_boot_chain_may_continue: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def evaluate_post_boot_scheduler_verification(
    *,
    restart_intent_hash: str,
    post_boot_wsl_decision_hash: str,
    post_boot_wsl_verified: bool,
    scheduler_evidence: Any,
) -> PostBootSchedulerDecision:
    _require_hash(restart_intent_hash)
    _require_hash(post_boot_wsl_decision_hash)
    if not isinstance(post_boot_wsl_verified, bool):
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_FIELD_INVALID")
    if not isinstance(scheduler_evidence, SchedulerHealthEvidence):
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_EVIDENCE_TYPE_INVALID")
    try:
        validate_scheduler_health_evidence(scheduler_evidence)
    except ValueError as exc:
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_EVIDENCE_INVALID") from exc

    if not post_boot_wsl_verified:
        status: VerificationStatus = "INCOMPLETE"
        reasons = ["POST_BOOT_WSL_PREREQUISITE_NOT_VERIFIED"]
    elif scheduler_evidence.status in {"STALE", "INCOMPLETE"}:
        status = "INCOMPLETE"
        reasons = [f"POST_BOOT_SCHEDULER_{reason}" for reason in scheduler_evidence.reasons]
    elif scheduler_evidence.status == "IDENTITY_MISMATCH":
        status = "TAMPERED"
        reasons = ["POST_BOOT_SCHEDULER_IDENTITY_MISMATCH"]
    elif scheduler_evidence.status != "HEALTHY":
        status = "FAIL"
        reasons = [f"POST_BOOT_SCHEDULER_{reason}" for reason in scheduler_evidence.reasons]
    else:
        status = "PASS"
        reasons = []
    passed = status == "PASS"
    unsigned = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "restart_intent_hash": restart_intent_hash,
        "post_boot_wsl_decision_hash": post_boot_wsl_decision_hash,
        "scheduler_evidence_hash": scheduler_evidence.evidence_hash,
        "read_only": True,
        "post_boot_scheduler_verified": passed,
        "post_boot_chain_may_continue": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return PostBootSchedulerDecision(
        status=status,
        reasons=tuple(reasons),
        restart_intent_hash=restart_intent_hash,
        post_boot_wsl_decision_hash=post_boot_wsl_decision_hash,
        scheduler_evidence_hash=scheduler_evidence.evidence_hash,
        decision_hash=_hash(unsigned),
        post_boot_scheduler_verified=passed,
        post_boot_chain_may_continue=passed,
    )


def validate_post_boot_scheduler_decision(value: Any) -> None:
    if not isinstance(value, PostBootSchedulerDecision):
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_DECISION_TYPE_INVALID")
    passed = value.status == "PASS"
    if (
        value.read_only is not True
        or value.post_boot_scheduler_verified != passed
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
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_DECISION_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PostBootSchedulerVerificationError("POST_BOOT_SCHEDULER_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
