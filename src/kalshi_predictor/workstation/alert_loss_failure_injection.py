from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

INJECTION_SCHEMA_VERSION = "phase4kp-alert-loss-failure-injection-v1"
InjectionStatus = Literal["PASSED", "FAILED", "INCOMPLETE", "TAMPERED"]


class AlertLossFailureInjectionError(ValueError):
    """Stable fail-closed alert-loss injection error."""


@dataclass(frozen=True)
class AlertLossInjectionEvidence:
    incident_hash: str
    alert_payload_hash: str
    primary_channel_hash: str
    secondary_channel_hash: str
    primary_delivery_suppressed: bool
    primary_acknowledgement_observed: bool
    secondary_escalation_generated: bool
    recovery_progressed: bool
    restart_progressed: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class AlertLossInjectionResult:
    status: InjectionStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    result_hash: str
    alert_loss_injected: bool = False
    escalation_proven: bool = False
    fail_closed_proven: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    alert_delivery_authorized: bool = False
    execution_authorized: bool = False


def make_alert_loss_injection_evidence(**fields: Any) -> AlertLossInjectionEvidence:
    _validate_fields(fields)
    return AlertLossInjectionEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_alert_loss_failure_injection(evidence: Any) -> AlertLossInjectionResult:
    item = _validated_evidence(evidence)
    if not item.complete:
        status: InjectionStatus = "INCOMPLETE"
        reasons = ["ALERT_LOSS_INJECTION_EVIDENCE_INCOMPLETE"]
    elif not item.primary_delivery_suppressed:
        status = "INCOMPLETE"
        reasons = ["PRIMARY_ALERT_LOSS_NOT_INJECTED"]
    elif item.primary_acknowledgement_observed:
        status = "TAMPERED"
        reasons = ["SUPPRESSED_PRIMARY_ALERT_ACKNOWLEDGED"]
    else:
        failures = []
        if not item.secondary_escalation_generated:
            failures.append("ALERT_LOSS_ESCALATION_MISSING")
        if item.recovery_progressed:
            failures.append("RECOVERY_PROGRESSED_WITHOUT_ALERT")
        if item.restart_progressed:
            failures.append("RESTART_PROGRESSED_WITHOUT_ALERT")
        status = "FAILED" if failures else "PASSED"
        reasons = failures
    passed = status == "PASSED"
    unsigned = {
        "schema_version": INJECTION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "alert_loss_injected": item.primary_delivery_suppressed,
        "escalation_proven": passed and item.secondary_escalation_generated,
        "fail_closed_proven": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "alert_delivery_authorized": False,
        "execution_authorized": False,
    }
    return AlertLossInjectionResult(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        result_hash=_hash(unsigned),
        alert_loss_injected=item.primary_delivery_suppressed,
        escalation_proven=passed and item.secondary_escalation_generated,
        fail_closed_proven=passed,
    )


def validate_alert_loss_injection_result(value: Any) -> None:
    if not isinstance(value, AlertLossInjectionResult):
        raise AlertLossFailureInjectionError("ALERT_LOSS_RESULT_TYPE_INVALID")
    passed = value.status == "PASSED"
    if (
        value.fail_closed_proven != passed
        or value.escalation_proven != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.alert_delivery_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise AlertLossFailureInjectionError("ALERT_LOSS_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = INJECTION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise AlertLossFailureInjectionError("ALERT_LOSS_RESULT_HASH_MISMATCH")


def _validated_evidence(value: Any) -> AlertLossInjectionEvidence:
    if not isinstance(value, AlertLossInjectionEvidence):
        raise AlertLossFailureInjectionError("ALERT_LOSS_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise AlertLossFailureInjectionError("ALERT_LOSS_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_hash",
        "alert_payload_hash",
        "primary_channel_hash",
        "secondary_channel_hash",
        "primary_delivery_suppressed",
        "primary_acknowledgement_observed",
        "secondary_escalation_generated",
        "recovery_progressed",
        "restart_progressed",
        "complete",
    }
    if set(fields) != required:
        raise AlertLossFailureInjectionError("ALERT_LOSS_EVIDENCE_FIELD_INVALID")
    for key in (
        "incident_hash",
        "alert_payload_hash",
        "primary_channel_hash",
        "secondary_channel_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise AlertLossFailureInjectionError("ALERT_LOSS_EVIDENCE_FIELD_INVALID")
    for key in required - {
        "incident_hash",
        "alert_payload_hash",
        "primary_channel_hash",
        "secondary_channel_hash",
    }:
        if not isinstance(fields[key], bool):
            raise AlertLossFailureInjectionError("ALERT_LOSS_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
