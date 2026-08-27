from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .recovery_evidence_canonicalization import (
    CanonicalRecoveryEvidenceBundle,
    validate_canonical_recovery_evidence_bundle,
)

DETECTOR_SCHEMA_VERSION = "phase4hj-recovery-decision-tamper-detection-v1"
DecisionAction = Literal["AWAIT_RECOVERY_POLICY", "DENY_RECOVERY"]
DetectionStatus = Literal["VERIFIED", "TAMPERED", "STALE", "INCOMPLETE", "DENIED"]


class RecoveryDecisionTamperDetectionError(ValueError):
    """Stable fail-closed recovery decision tamper-detection error."""


@dataclass(frozen=True)
class RecoveryDecisionRecord:
    decision_id: str
    created_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    action: DecisionAction
    target: str
    bundle_hash: str
    prerequisites_ready_claim: bool
    complete: bool
    source_identity_hash: str
    decision_hash: str


@dataclass(frozen=True)
class RecoveryDecisionTamperReport:
    status: DetectionStatus
    reasons: tuple[str, ...]
    decision_id_hash: str
    decision_hash: str
    bundle_hash: str
    action: DecisionAction
    target: str
    created_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    evaluated_at_epoch_seconds: int
    max_ttl_seconds: int
    source_identity_hash: str
    report_hash: str
    read_only: bool = True
    decision_integrity_proven: bool = False
    bundle_binding_proven: bool = False
    readiness_claim_proven: bool = False
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_decision_record(
    *,
    decision_id: str,
    created_at_epoch_seconds: int,
    expires_at_epoch_seconds: int,
    action: DecisionAction,
    target: str,
    bundle_hash: str,
    prerequisites_ready_claim: bool,
    complete: bool,
    source_identity_hash: str,
) -> RecoveryDecisionRecord:
    unsigned = {
        "decision_id": decision_id,
        "created_at_epoch_seconds": created_at_epoch_seconds,
        "expires_at_epoch_seconds": expires_at_epoch_seconds,
        "action": action,
        "target": target,
        "bundle_hash": bundle_hash,
        "prerequisites_ready_claim": prerequisites_ready_claim,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
    }
    _validate_decision_fields(unsigned)
    return RecoveryDecisionRecord(**unsigned, decision_hash=_hash(unsigned))


def detect_recovery_decision_tampering(
    bundle: Any,
    decision: Any,
    *,
    evaluated_at_epoch_seconds: int,
    max_ttl_seconds: int = 300,
) -> RecoveryDecisionTamperReport:
    if (
        isinstance(evaluated_at_epoch_seconds, bool)
        or not isinstance(evaluated_at_epoch_seconds, int)
        or evaluated_at_epoch_seconds < 0
        or isinstance(max_ttl_seconds, bool)
        or not isinstance(max_ttl_seconds, int)
        or max_ttl_seconds <= 0
    ):
        raise RecoveryDecisionTamperDetectionError("DETECTOR_BOUND_INVALID")
    if not isinstance(bundle, CanonicalRecoveryEvidenceBundle):
        raise RecoveryDecisionTamperDetectionError("BUNDLE_TYPE_INVALID")
    try:
        validate_canonical_recovery_evidence_bundle(bundle)
    except ValueError as exc:
        raise RecoveryDecisionTamperDetectionError("BUNDLE_INVALID") from exc
    item = _validated_decision(decision)
    bundle_bound = item.bundle_hash == bundle.bundle_hash
    readiness_proven = item.prerequisites_ready_claim == bundle.prerequisites_ready
    expected_action: DecisionAction = (
        "AWAIT_RECOVERY_POLICY" if bundle.prerequisites_ready else "DENY_RECOVERY"
    )
    ttl = item.expires_at_epoch_seconds - item.created_at_epoch_seconds

    if not item.complete:
        status: DetectionStatus = "INCOMPLETE"
        reasons = ["RECOVERY_DECISION_INCOMPLETE"]
    elif not bundle_bound or not readiness_proven:
        status = "TAMPERED"
        reasons = []
        if not bundle_bound:
            reasons.append("RECOVERY_DECISION_BUNDLE_MISMATCH")
        if not readiness_proven:
            reasons.append("RECOVERY_DECISION_READINESS_CLAIM_MISMATCH")
    elif item.created_at_epoch_seconds > evaluated_at_epoch_seconds:
        status = "DENIED"
        reasons = ["RECOVERY_DECISION_FROM_FUTURE"]
    elif ttl < 0 or ttl > max_ttl_seconds:
        status = "DENIED"
        reasons = ["RECOVERY_DECISION_TTL_INVALID"]
    elif evaluated_at_epoch_seconds > item.expires_at_epoch_seconds:
        status = "STALE"
        reasons = ["RECOVERY_DECISION_EXPIRED"]
    elif item.action != expected_action:
        status = "DENIED"
        reasons = ["RECOVERY_DECISION_ACTION_INVALID"]
    else:
        status = "VERIFIED"
        reasons = []

    verified = status == "VERIFIED"
    unsigned = {
        "schema_version": DETECTOR_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "decision_id_hash": _hash(item.decision_id),
        "decision_hash": item.decision_hash,
        "bundle_hash": bundle.bundle_hash,
        "action": item.action,
        "target": item.target,
        "created_at_epoch_seconds": item.created_at_epoch_seconds,
        "expires_at_epoch_seconds": item.expires_at_epoch_seconds,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "max_ttl_seconds": max_ttl_seconds,
        "source_identity_hash": item.source_identity_hash,
        "read_only": True,
        "decision_integrity_proven": True,
        "bundle_binding_proven": bundle_bound,
        "readiness_claim_proven": readiness_proven,
        "alert_required": not verified,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return RecoveryDecisionTamperReport(
        status=status,
        reasons=tuple(reasons),
        decision_id_hash=unsigned["decision_id_hash"],
        decision_hash=item.decision_hash,
        bundle_hash=bundle.bundle_hash,
        action=item.action,
        target=item.target,
        created_at_epoch_seconds=item.created_at_epoch_seconds,
        expires_at_epoch_seconds=item.expires_at_epoch_seconds,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        max_ttl_seconds=max_ttl_seconds,
        source_identity_hash=item.source_identity_hash,
        report_hash=_hash(unsigned),
        decision_integrity_proven=True,
        bundle_binding_proven=bundle_bound,
        readiness_claim_proven=readiness_proven,
        alert_required=not verified,
    )


def validate_recovery_decision_tamper_report(report: Any) -> None:
    if not isinstance(report, RecoveryDecisionTamperReport):
        raise RecoveryDecisionTamperDetectionError("REPORT_TYPE_INVALID")
    if report.read_only is not True or any(
        (
            report.recovery_authorized,
            report.service_control_authorized,
            report.host_restart_authorized,
            report.execution_authorized,
        )
    ):
        raise RecoveryDecisionTamperDetectionError("REPORT_SAFETY_BOUNDARY_INVALID")
    verified = (
        not report.reasons
        and not report.alert_required
        and report.decision_integrity_proven
        and report.bundle_binding_proven
        and report.readiness_claim_proven
    )
    if (report.status == "VERIFIED") != verified:
        raise RecoveryDecisionTamperDetectionError("REPORT_STATUS_INVALID")
    unsigned = asdict(report)
    unsigned.pop("report_hash")
    unsigned["schema_version"] = DETECTOR_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if report.report_hash != _hash(unsigned):
        raise RecoveryDecisionTamperDetectionError("REPORT_HASH_MISMATCH")


def _validated_decision(value: Any) -> RecoveryDecisionRecord:
    if not isinstance(value, RecoveryDecisionRecord):
        raise RecoveryDecisionTamperDetectionError("DECISION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("decision_hash")
    _validate_decision_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise RecoveryDecisionTamperDetectionError("DECISION_HASH_MISMATCH")
    return value


def _validate_decision_fields(payload: dict[str, Any]) -> None:
    if payload["action"] not in {"AWAIT_RECOVERY_POLICY", "DENY_RECOVERY"}:
        raise RecoveryDecisionTamperDetectionError("DECISION_FIELD_INVALID")
    for key in ("decision_id", "target", "bundle_hash", "source_identity_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise RecoveryDecisionTamperDetectionError("DECISION_FIELD_INVALID")
    for key in ("prerequisites_ready_claim", "complete"):
        if not isinstance(payload[key], bool):
            raise RecoveryDecisionTamperDetectionError("DECISION_FIELD_INVALID")
    for key in ("created_at_epoch_seconds", "expires_at_epoch_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecoveryDecisionTamperDetectionError("DECISION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
