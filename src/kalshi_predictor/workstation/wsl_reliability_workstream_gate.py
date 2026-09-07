from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .recovery_decision_tamper_detection import (
    RecoveryDecisionTamperReport,
    validate_recovery_decision_tamper_report,
)
from .recovery_evidence_canonicalization import (
    CanonicalRecoveryEvidenceBundle,
    validate_canonical_recovery_evidence_bundle,
)
from .wsl_keepalive_reliability_audit import (
    WslKeepaliveReliabilityAudit,
    validate_wsl_keepalive_reliability_audit,
)

GATE_SCHEMA_VERSION = "phase4hk-wsl-reliability-workstream-gate-v1"
GateStatus = Literal["CERTIFIED", "DENIED", "STALE", "INCOMPLETE"]


class WslReliabilityWorkstreamGateError(ValueError):
    """Stable fail-closed WSL reliability workstream gate error."""


@dataclass(frozen=True)
class WslReliabilityWorkstreamGateResult:
    status: GateStatus
    reasons: tuple[str, ...]
    keepalive_audit_hash: str
    canonical_bundle_hash: str
    decision_report_hash: str
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    evaluated_at_epoch_seconds: int
    evidence_chain_hash: str
    gate_hash: str
    read_only: bool = True
    evidence_chain_complete: bool = False
    workstream_certified: bool = False
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def evaluate_wsl_reliability_workstream_gate(
    keepalive_audit: Any,
    canonical_bundle: Any,
    decision_report: Any,
    *,
    evaluated_at_epoch_seconds: int,
    max_evidence_age_seconds: int = 120,
) -> WslReliabilityWorkstreamGateResult:
    for value in (evaluated_at_epoch_seconds, max_evidence_age_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WslReliabilityWorkstreamGateError("GATE_BOUND_INVALID")
    if not isinstance(keepalive_audit, WslKeepaliveReliabilityAudit):
        raise WslReliabilityWorkstreamGateError("KEEPALIVE_AUDIT_TYPE_INVALID")
    if not isinstance(canonical_bundle, CanonicalRecoveryEvidenceBundle):
        raise WslReliabilityWorkstreamGateError("CANONICAL_BUNDLE_TYPE_INVALID")
    if not isinstance(decision_report, RecoveryDecisionTamperReport):
        raise WslReliabilityWorkstreamGateError("DECISION_REPORT_TYPE_INVALID")
    _validate_upstream("KEEPALIVE_AUDIT", validate_wsl_keepalive_reliability_audit, keepalive_audit)
    _validate_upstream(
        "CANONICAL_BUNDLE", validate_canonical_recovery_evidence_bundle, canonical_bundle
    )
    _validate_upstream("DECISION_REPORT", validate_recovery_decision_tamper_report, decision_report)

    observed_age = max(
        keepalive_audit.observed_max_age_seconds,
        canonical_bundle.observed_max_age_seconds,
    )
    statuses = (
        keepalive_audit.status,
        canonical_bundle.status,
        decision_report.status,
    )
    keepalive_incomplete = any(
        reason.startswith("OBSERVATION_INCOMPLETE:") for reason in keepalive_audit.reasons
    )
    if observed_age > max_evidence_age_seconds or "STALE" in statuses:
        status: GateStatus = "STALE"
        reasons = ["WSL_RELIABILITY_EVIDENCE_STALE"]
    elif "INCOMPLETE" in statuses or keepalive_incomplete:
        status = "INCOMPLETE"
        reasons = ["WSL_RELIABILITY_EVIDENCE_INCOMPLETE"]
    else:
        reasons = []
        if keepalive_audit.status != "HEALTHY":
            reasons.append(f"KEEPALIVE_AUDIT_NOT_HEALTHY:{keepalive_audit.status}")
        if canonical_bundle.status != "READY" or not canonical_bundle.prerequisites_ready:
            reasons.append(f"CANONICAL_BUNDLE_NOT_READY:{canonical_bundle.status}")
        if decision_report.status != "VERIFIED":
            reasons.append(f"DECISION_REPORT_NOT_VERIFIED:{decision_report.status}")
        if decision_report.bundle_hash != canonical_bundle.bundle_hash:
            reasons.append("DECISION_REPORT_BUNDLE_BINDING_MISMATCH")
        if evaluated_at_epoch_seconds > decision_report.expires_at_epoch_seconds:
            reasons.append("DECISION_REPORT_EXPIRED")
        status = "DENIED" if reasons else "CERTIFIED"

    certified = status == "CERTIFIED"
    evidence_hashes = [
        keepalive_audit.audit_hash,
        canonical_bundle.bundle_hash,
        decision_report.report_hash,
    ]
    evidence_chain_hash = _hash(evidence_hashes)
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "keepalive_audit_hash": keepalive_audit.audit_hash,
        "canonical_bundle_hash": canonical_bundle.bundle_hash,
        "decision_report_hash": decision_report.report_hash,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "evidence_chain_hash": evidence_chain_hash,
        "read_only": True,
        "evidence_chain_complete": True,
        "workstream_certified": certified,
        "alert_required": not certified,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WslReliabilityWorkstreamGateResult(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        keepalive_audit_hash=keepalive_audit.audit_hash,
        canonical_bundle_hash=canonical_bundle.bundle_hash,
        decision_report_hash=decision_report.report_hash,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        evidence_chain_hash=evidence_chain_hash,
        gate_hash=_hash(unsigned),
        evidence_chain_complete=True,
        workstream_certified=certified,
        alert_required=not certified,
    )


def validate_wsl_reliability_workstream_gate_result(result: Any) -> None:
    if not isinstance(result, WslReliabilityWorkstreamGateResult):
        raise WslReliabilityWorkstreamGateError("GATE_RESULT_TYPE_INVALID")
    if result.read_only is not True or any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise WslReliabilityWorkstreamGateError("GATE_SAFETY_BOUNDARY_INVALID")
    certified = (
        not result.reasons
        and not result.alert_required
        and result.evidence_chain_complete
        and result.workstream_certified
    )
    if (result.status == "CERTIFIED") != certified:
        raise WslReliabilityWorkstreamGateError("GATE_STATUS_INVALID")
    unsigned = asdict(result)
    unsigned.pop("gate_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.gate_hash != _hash(unsigned):
        raise WslReliabilityWorkstreamGateError("GATE_HASH_MISMATCH")


def _validate_upstream(label: str, validator: Any, value: Any) -> None:
    try:
        validator(value)
    except ValueError as exc:
        raise WslReliabilityWorkstreamGateError(f"{label}_INVALID") from exc


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
