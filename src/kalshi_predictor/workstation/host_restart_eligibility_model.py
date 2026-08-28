from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

MODEL_SCHEMA_VERSION = "phase4iz-host-restart-eligibility-model-v1"
EligibilityStatus = Literal["ELIGIBLE", "DENIED", "INCOMPLETE", "TAMPERED"]


class HostRestartEligibilityModelError(ValueError):
    """Stable fail-closed host-restart eligibility model error."""


@dataclass(frozen=True)
class HostRestartEligibilityEvidence:
    incident_id_hash: str
    classifier_decision_hash: str
    recovery_decision_hash: str
    diagnostics_hash: str
    invariant_evidence_hash: str
    writer_evidence_hash: str
    classifier_status: str
    component_recovery_failed: bool
    evidence_complete: bool
    trading_fail_closed: bool
    invariants_unchanged: bool
    writer_exclusive: bool
    test_host: bool
    evidence_hash: str


@dataclass(frozen=True)
class HostRestartEligibilityDecision:
    status: EligibilityStatus
    reasons: tuple[str, ...]
    incident_id_hash: str
    evidence_hash: str
    decision_hash: str
    read_only: bool = True
    eligibility_proven: bool = False
    warning_required: bool = True
    cancellation_required: bool = True
    cooldown_check_required: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_host_restart_eligibility_evidence(
    **fields: Any,
) -> HostRestartEligibilityEvidence:
    _validate_fields(fields)
    return HostRestartEligibilityEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_host_restart_eligibility(
    evidence: Any,
) -> HostRestartEligibilityDecision:
    item = _validated_evidence(evidence)
    if not item.evidence_complete:
        status: EligibilityStatus = "INCOMPLETE"
        reasons = ["HOST_RESTART_EVIDENCE_INCOMPLETE"]
    else:
        reasons = []
        if item.classifier_status != "HOST_RESTART_REQUIRED":
            reasons.append(f"HOST_RESTART_CLASSIFIER_DENIED:{item.classifier_status}")
        if not item.component_recovery_failed:
            reasons.append("HOST_RESTART_COMPONENT_RECOVERY_NOT_FAILED")
        if not item.trading_fail_closed:
            reasons.append("HOST_RESTART_TRADING_NOT_FAIL_CLOSED")
        if not item.invariants_unchanged:
            reasons.append("HOST_RESTART_INVARIANT_CHANGED")
        if not item.writer_exclusive:
            reasons.append("HOST_RESTART_WRITER_EXCLUSIVITY_UNPROVEN")
        if item.test_host:
            reasons.append("HOST_RESTART_TEST_HOST_DENIED")
        status = "DENIED" if reasons else "ELIGIBLE"
    eligible = status == "ELIGIBLE"
    unsigned = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "incident_id_hash": item.incident_id_hash,
        "evidence_hash": item.evidence_hash,
        "read_only": True,
        "eligibility_proven": eligible,
        "warning_required": True,
        "cancellation_required": True,
        "cooldown_check_required": True,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return HostRestartEligibilityDecision(
        status=status,
        reasons=tuple(reasons),
        incident_id_hash=item.incident_id_hash,
        evidence_hash=item.evidence_hash,
        decision_hash=_hash(unsigned),
        eligibility_proven=eligible,
    )


def validate_host_restart_eligibility_decision(value: Any) -> None:
    if not isinstance(value, HostRestartEligibilityDecision):
        raise HostRestartEligibilityModelError("HOST_RESTART_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.warning_required is not True
        or value.cancellation_required is not True
        or value.cooldown_check_required is not True
        or any(
            (
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise HostRestartEligibilityModelError("HOST_RESTART_SAFETY_BOUNDARY_INVALID")
    if value.eligibility_proven != (value.status == "ELIGIBLE"):
        raise HostRestartEligibilityModelError("HOST_RESTART_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = MODEL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise HostRestartEligibilityModelError("HOST_RESTART_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> HostRestartEligibilityEvidence:
    if not isinstance(value, HostRestartEligibilityEvidence):
        raise HostRestartEligibilityModelError("HOST_RESTART_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise HostRestartEligibilityModelError("HOST_RESTART_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_id_hash",
        "classifier_decision_hash",
        "recovery_decision_hash",
        "diagnostics_hash",
        "invariant_evidence_hash",
        "writer_evidence_hash",
        "classifier_status",
        "component_recovery_failed",
        "evidence_complete",
        "trading_fail_closed",
        "invariants_unchanged",
        "writer_exclusive",
        "test_host",
    }
    if set(fields) != required:
        raise HostRestartEligibilityModelError("HOST_RESTART_EVIDENCE_FIELD_INVALID")
    for key in (
        "incident_id_hash",
        "classifier_decision_hash",
        "recovery_decision_hash",
        "diagnostics_hash",
        "invariant_evidence_hash",
        "writer_evidence_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise HostRestartEligibilityModelError("HOST_RESTART_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["classifier_status"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["classifier_status"]) is None
    ):
        raise HostRestartEligibilityModelError("HOST_RESTART_EVIDENCE_FIELD_INVALID")
    for key in (
        "component_recovery_failed",
        "evidence_complete",
        "trading_fail_closed",
        "invariants_unchanged",
        "writer_exclusive",
        "test_host",
    ):
        if not isinstance(fields[key], bool):
            raise HostRestartEligibilityModelError("HOST_RESTART_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
