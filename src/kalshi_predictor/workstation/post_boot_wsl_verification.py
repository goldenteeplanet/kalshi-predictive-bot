from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

VERIFICATION_SCHEMA_VERSION = "phase4kd-post-boot-wsl-verification-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class PostBootWslVerificationError(ValueError):
    """Stable fail-closed post-boot WSL verification error."""


@dataclass(frozen=True)
class PostBootWslEvidence:
    restart_intent_hash: str
    pre_boot_identity_hash: str
    post_boot_identity_hash: str
    wsl_probe_hash: str
    authoritative_distro_hash: str
    intent_created_at_epoch: int
    observed_at_epoch: int
    wsl_available: bool
    authoritative_distro_running: bool
    probe_integrity_verified: bool
    evidence_complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class PostBootWslDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    restart_intent_hash: str
    post_boot_identity_hash: str
    evidence_hash: str
    observation_delay_seconds: int
    decision_hash: str
    read_only: bool = True
    post_boot_wsl_verified: bool = False
    post_boot_chain_may_continue: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_post_boot_wsl_evidence(**fields: Any) -> PostBootWslEvidence:
    _validate_fields(fields)
    return PostBootWslEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_post_boot_wsl_verification(evidence: Any) -> PostBootWslDecision:
    item = _validated_evidence(evidence)
    boot_unchanged = item.pre_boot_identity_hash == item.post_boot_identity_hash
    time_reversed = item.observed_at_epoch < item.intent_created_at_epoch
    if boot_unchanged or time_reversed:
        status: VerificationStatus = "TAMPERED"
        reasons = []
        if boot_unchanged:
            reasons.append("POST_BOOT_WSL_BOOT_IDENTITY_UNCHANGED")
        if time_reversed:
            reasons.append("POST_BOOT_WSL_OBSERVATION_BEFORE_INTENT")
    elif not item.evidence_complete or not item.probe_integrity_verified:
        status = "INCOMPLETE"
        reasons = ["POST_BOOT_WSL_EVIDENCE_UNTRUSTED"]
    else:
        reasons = []
        if not item.wsl_available:
            reasons.append("POST_BOOT_WSL_UNAVAILABLE")
        if not item.authoritative_distro_running:
            reasons.append("POST_BOOT_WSL_AUTHORITATIVE_DISTRO_NOT_RUNNING")
        status = "FAIL" if reasons else "PASS"
    passed = status == "PASS"
    delay = max(0, item.observed_at_epoch - item.intent_created_at_epoch)
    unsigned = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "restart_intent_hash": item.restart_intent_hash,
        "post_boot_identity_hash": item.post_boot_identity_hash,
        "evidence_hash": item.evidence_hash,
        "observation_delay_seconds": delay,
        "read_only": True,
        "post_boot_wsl_verified": passed,
        "post_boot_chain_may_continue": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return PostBootWslDecision(
        status=status,
        reasons=tuple(reasons),
        restart_intent_hash=item.restart_intent_hash,
        post_boot_identity_hash=item.post_boot_identity_hash,
        evidence_hash=item.evidence_hash,
        observation_delay_seconds=delay,
        decision_hash=_hash(unsigned),
        post_boot_wsl_verified=passed,
        post_boot_chain_may_continue=passed,
    )


def validate_post_boot_wsl_decision(value: Any) -> None:
    if not isinstance(value, PostBootWslDecision):
        raise PostBootWslVerificationError("POST_BOOT_WSL_DECISION_TYPE_INVALID")
    passed = value.status == "PASS"
    if (
        value.read_only is not True
        or value.post_boot_wsl_verified != passed
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
        raise PostBootWslVerificationError("POST_BOOT_WSL_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise PostBootWslVerificationError("POST_BOOT_WSL_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> PostBootWslEvidence:
    if not isinstance(value, PostBootWslEvidence):
        raise PostBootWslVerificationError("POST_BOOT_WSL_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise PostBootWslVerificationError("POST_BOOT_WSL_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "restart_intent_hash",
        "pre_boot_identity_hash",
        "post_boot_identity_hash",
        "wsl_probe_hash",
        "authoritative_distro_hash",
        "intent_created_at_epoch",
        "observed_at_epoch",
        "wsl_available",
        "authoritative_distro_running",
        "probe_integrity_verified",
        "evidence_complete",
    }
    if set(fields) != required:
        raise PostBootWslVerificationError("POST_BOOT_WSL_EVIDENCE_FIELD_INVALID")
    for key in (
        "restart_intent_hash",
        "pre_boot_identity_hash",
        "post_boot_identity_hash",
        "wsl_probe_hash",
        "authoritative_distro_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise PostBootWslVerificationError("POST_BOOT_WSL_EVIDENCE_FIELD_INVALID")
    for key in ("intent_created_at_epoch", "observed_at_epoch"):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise PostBootWslVerificationError("POST_BOOT_WSL_EVIDENCE_FIELD_INVALID")
    for key in (
        "wsl_available",
        "authoritative_distro_running",
        "probe_integrity_verified",
        "evidence_complete",
    ):
        if not isinstance(fields[key], bool):
            raise PostBootWslVerificationError("POST_BOOT_WSL_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
