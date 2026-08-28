from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

RECOVERY_SCHEMA_VERSION = "phase4jq-supervisor-crash-recovery-v1"
OwnerStatus = Literal["LIVE", "DEAD", "UNKNOWN"]
RecoveryStatus = Literal["HEALTHY", "RECOVERY_REQUIRED", "DENIED", "TAMPERED"]
Disposition = Literal[
    "KEEP_EXISTING_OWNER",
    "RECONCILE_INTENT_AND_LOCK",
    "RECONCILE_LOCK_ONLY",
    "REFUSE_UNKNOWN_OWNER",
    "REFUSE_UNTRUSTED_STATE",
]


class SupervisorCrashRecoveryError(ValueError):
    """Stable fail-closed supervisor crash recovery error."""


@dataclass(frozen=True)
class SupervisorCrashEvidence:
    lock_record_hash: str
    owner_liveness_hash: str
    restart_intent_hash: str
    owner_status: str
    lock_valid: bool
    durable_intent_present: bool
    post_boot_verification_pending: bool
    evidence_complete: bool
    integrity_verified: bool
    evidence_hash: str


@dataclass(frozen=True)
class SupervisorCrashRecoveryDecision:
    status: RecoveryStatus
    reasons: tuple[str, ...]
    disposition: Disposition
    evidence_hash: str
    decision_hash: str
    read_only: bool = True
    operator_reconciliation_required: bool = False
    lock_release_authorized: bool = False
    lock_steal_authorized: bool = False
    automatic_replay_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_supervisor_crash_evidence(**fields: Any) -> SupervisorCrashEvidence:
    _validate_fields(fields)
    return SupervisorCrashEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_supervisor_crash_recovery(evidence: Any) -> SupervisorCrashRecoveryDecision:
    item = _validated_evidence(evidence)
    contradiction = item.post_boot_verification_pending and not item.durable_intent_present
    if not item.evidence_complete or not item.integrity_verified or not item.lock_valid:
        status: RecoveryStatus = "DENIED"
        reasons = ["SUPERVISOR_CRASH_STATE_UNTRUSTED"]
        disposition: Disposition = "REFUSE_UNTRUSTED_STATE"
    elif contradiction:
        status = "TAMPERED"
        reasons = ["SUPERVISOR_CRASH_INTENT_STATE_CONTRADICTION"]
        disposition = "REFUSE_UNTRUSTED_STATE"
    elif item.owner_status == "UNKNOWN":
        status = "DENIED"
        reasons = ["SUPERVISOR_CRASH_OWNER_UNKNOWN"]
        disposition = "REFUSE_UNKNOWN_OWNER"
    elif item.owner_status == "LIVE":
        status = "HEALTHY"
        reasons = []
        disposition = "KEEP_EXISTING_OWNER"
    elif item.durable_intent_present:
        status = "RECOVERY_REQUIRED"
        reasons = ["SUPERVISOR_CRASH_DURABLE_INTENT_RECONCILIATION_REQUIRED"]
        disposition = "RECONCILE_INTENT_AND_LOCK"
    else:
        status = "RECOVERY_REQUIRED"
        reasons = ["SUPERVISOR_CRASH_LOCK_RECONCILIATION_REQUIRED"]
        disposition = "RECONCILE_LOCK_ONLY"
    reconcile = status == "RECOVERY_REQUIRED"
    unsigned = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "disposition": disposition,
        "evidence_hash": item.evidence_hash,
        "read_only": True,
        "operator_reconciliation_required": reconcile,
        "lock_release_authorized": False,
        "lock_steal_authorized": False,
        "automatic_replay_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SupervisorCrashRecoveryDecision(
        status=status,
        reasons=tuple(reasons),
        disposition=disposition,
        evidence_hash=item.evidence_hash,
        decision_hash=_hash(unsigned),
        operator_reconciliation_required=reconcile,
    )


def validate_supervisor_crash_recovery_decision(value: Any) -> None:
    if not isinstance(value, SupervisorCrashRecoveryDecision):
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.operator_reconciliation_required != (value.status == "RECOVERY_REQUIRED")
        or any(
            (
                value.lock_release_authorized,
                value.lock_steal_authorized,
                value.automatic_replay_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = RECOVERY_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> SupervisorCrashEvidence:
    if not isinstance(value, SupervisorCrashEvidence):
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "lock_record_hash",
        "owner_liveness_hash",
        "restart_intent_hash",
        "owner_status",
        "lock_valid",
        "durable_intent_present",
        "post_boot_verification_pending",
        "evidence_complete",
        "integrity_verified",
    }
    if set(fields) != required:
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_EVIDENCE_FIELD_INVALID")
    for key in ("lock_record_hash", "owner_liveness_hash", "restart_intent_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_EVIDENCE_FIELD_INVALID")
    if fields["owner_status"] not in {"LIVE", "DEAD", "UNKNOWN"}:
        raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_EVIDENCE_FIELD_INVALID")
    for key in (
        "lock_valid",
        "durable_intent_present",
        "post_boot_verification_pending",
        "evidence_complete",
        "integrity_verified",
    ):
        if not isinstance(fields[key], bool):
            raise SupervisorCrashRecoveryError("SUPERVISOR_CRASH_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
