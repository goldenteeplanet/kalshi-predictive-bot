from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

SCHEMA_VERSION = "phase4kr-scheduler-failure-injection-v1"
Status = Literal["PASSED", "FAILED", "INCOMPLETE", "TAMPERED"]


class SchedulerFailureInjectionError(ValueError):
    """Stable fail-closed scheduler-failure injection error."""


@dataclass(frozen=True)
class SchedulerFailureEvidence:
    incident_hash: str
    scheduler_identity_hash: str
    failure_injected: bool
    failure_detected: bool
    recovery_attempt_count: int
    recovery_verification_performed: bool
    recovery_verified: bool
    escalation_generated: bool
    uncontrolled_restart_progressed: bool
    writer_count_after: int
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class SchedulerFailureResult:
    status: Status
    reasons: tuple[str, ...]
    evidence_hash: str
    result_hash: str
    bounded_recovery_proven: bool = False
    escalation_proven: bool = False
    writer_safety_proven: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_scheduler_failure_evidence(**fields: Any) -> SchedulerFailureEvidence:
    _validate_fields(fields)
    return SchedulerFailureEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_scheduler_failure_injection(evidence: Any) -> SchedulerFailureResult:
    item = _validated(evidence)
    if not item.complete:
        status: Status = "INCOMPLETE"
        reasons = ["SCHEDULER_FAILURE_EVIDENCE_INCOMPLETE"]
    elif not item.failure_injected:
        status = "INCOMPLETE"
        reasons = ["SCHEDULER_FAILURE_NOT_INJECTED"]
    elif item.recovery_verified and not item.recovery_verification_performed:
        status = "TAMPERED"
        reasons = ["SCHEDULER_RECOVERY_VERIFIED_WITHOUT_CHECK"]
    else:
        failures = []
        if not item.failure_detected:
            failures.append("SCHEDULER_FAILURE_NOT_DETECTED")
        if item.recovery_attempt_count != 1:
            failures.append("SCHEDULER_RECOVERY_ATTEMPT_NOT_EXACTLY_ONCE")
        if not item.recovery_verification_performed:
            failures.append("SCHEDULER_RECOVERY_NOT_VERIFIED")
        if not item.recovery_verified and not item.escalation_generated:
            failures.append("SCHEDULER_RECOVERY_FAILURE_NOT_ESCALATED")
        if item.uncontrolled_restart_progressed:
            failures.append("UNCONTROLLED_RESTART_PROGRESSED")
        expected_writers = 1 if item.recovery_verified else 0
        if item.writer_count_after != expected_writers:
            failures.append("SCHEDULER_WRITER_STATE_INCOHERENT")
        status = "FAILED" if failures else "PASSED"
        reasons = failures
    passed = status == "PASSED"
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "bounded_recovery_proven": passed,
        "escalation_proven": passed and (item.recovery_verified or item.escalation_generated),
        "writer_safety_proven": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SchedulerFailureResult(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        result_hash=_hash(unsigned),
        bounded_recovery_proven=passed,
        escalation_proven=passed and (item.recovery_verified or item.escalation_generated),
        writer_safety_proven=passed,
    )


def validate_scheduler_failure_result(value: Any) -> None:
    if not isinstance(value, SchedulerFailureResult):
        raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_RESULT_TYPE_INVALID")
    passed = value.status == "PASSED"
    if (
        value.bounded_recovery_proven != passed
        or value.writer_safety_proven != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_RESULT_HASH_MISMATCH")


def _validated(value: Any) -> SchedulerFailureEvidence:
    if not isinstance(value, SchedulerFailureEvidence):
        raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_hash",
        "scheduler_identity_hash",
        "failure_injected",
        "failure_detected",
        "recovery_attempt_count",
        "recovery_verification_performed",
        "recovery_verified",
        "escalation_generated",
        "uncontrolled_restart_progressed",
        "writer_count_after",
        "complete",
    }
    if set(fields) != required:
        raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_EVIDENCE_FIELD_INVALID")
    for key in ("incident_hash", "scheduler_identity_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_EVIDENCE_FIELD_INVALID")
    for key in ("recovery_attempt_count", "writer_count_after"):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_EVIDENCE_FIELD_INVALID")
    for key in required - {
        "incident_hash",
        "scheduler_identity_hash",
        "recovery_attempt_count",
        "writer_count_after",
    }:
        if not isinstance(fields[key], bool):
            raise SchedulerFailureInjectionError("SCHEDULER_FAILURE_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
