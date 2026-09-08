from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

INJECTION_SCHEMA_VERSION = "phase4kq-wsl-hang-failure-injection-v1"
InjectionStatus = Literal["PASSED", "FAILED", "INCOMPLETE", "TAMPERED"]


class WslHangFailureInjectionError(ValueError):
    """Stable fail-closed WSL-hang injection error."""


@dataclass(frozen=True)
class WslHangInjectionEvidence:
    incident_hash: str
    probe_identity_hash: str
    timeout_milliseconds: int
    observed_duration_milliseconds: int
    hang_injected: bool
    timeout_observed: bool
    probe_terminated: bool
    escalation_generated: bool
    recovery_progressed: bool
    restart_progressed: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class WslHangInjectionResult:
    status: InjectionStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    result_hash: str
    hang_injected: bool = False
    bounded_termination_proven: bool = False
    fail_closed_proven: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    process_control_authorized: bool = False
    execution_authorized: bool = False


def make_wsl_hang_injection_evidence(**fields: Any) -> WslHangInjectionEvidence:
    _validate_fields(fields)
    return WslHangInjectionEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_wsl_hang_failure_injection(evidence: Any) -> WslHangInjectionResult:
    item = _validated_evidence(evidence)
    if not item.complete:
        status: InjectionStatus = "INCOMPLETE"
        reasons = ["WSL_HANG_INJECTION_EVIDENCE_INCOMPLETE"]
    elif not item.hang_injected:
        status = "INCOMPLETE"
        reasons = ["WSL_HANG_NOT_INJECTED"]
    elif item.timeout_observed and item.observed_duration_milliseconds < item.timeout_milliseconds:
        status = "TAMPERED"
        reasons = ["WSL_TIMEOUT_BEFORE_BOUND"]
    else:
        failures = []
        if not item.timeout_observed:
            failures.append("WSL_HANG_TIMEOUT_NOT_OBSERVED")
        if not item.probe_terminated:
            failures.append("WSL_HUNG_PROBE_NOT_TERMINATED")
        if not item.escalation_generated:
            failures.append("WSL_HANG_ESCALATION_MISSING")
        if item.recovery_progressed:
            failures.append("RECOVERY_PROGRESSED_AFTER_WSL_HANG")
        if item.restart_progressed:
            failures.append("RESTART_PROGRESSED_AFTER_WSL_HANG")
        status = "FAILED" if failures else "PASSED"
        reasons = failures
    passed = status == "PASSED"
    unsigned = {
        "schema_version": INJECTION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "hang_injected": item.hang_injected,
        "bounded_termination_proven": passed,
        "fail_closed_proven": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "process_control_authorized": False,
        "execution_authorized": False,
    }
    return WslHangInjectionResult(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        result_hash=_hash(unsigned),
        hang_injected=item.hang_injected,
        bounded_termination_proven=passed,
        fail_closed_proven=passed,
    )


def validate_wsl_hang_injection_result(value: Any) -> None:
    if not isinstance(value, WslHangInjectionResult):
        raise WslHangFailureInjectionError("WSL_HANG_RESULT_TYPE_INVALID")
    passed = value.status == "PASSED"
    if (
        value.bounded_termination_proven != passed
        or value.fail_closed_proven != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.process_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise WslHangFailureInjectionError("WSL_HANG_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = INJECTION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise WslHangFailureInjectionError("WSL_HANG_RESULT_HASH_MISMATCH")


def _validated_evidence(value: Any) -> WslHangInjectionEvidence:
    if not isinstance(value, WslHangInjectionEvidence):
        raise WslHangFailureInjectionError("WSL_HANG_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise WslHangFailureInjectionError("WSL_HANG_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_hash",
        "probe_identity_hash",
        "timeout_milliseconds",
        "observed_duration_milliseconds",
        "hang_injected",
        "timeout_observed",
        "probe_terminated",
        "escalation_generated",
        "recovery_progressed",
        "restart_progressed",
        "complete",
    }
    if set(fields) != required:
        raise WslHangFailureInjectionError("WSL_HANG_EVIDENCE_FIELD_INVALID")
    for key in ("incident_hash", "probe_identity_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise WslHangFailureInjectionError("WSL_HANG_EVIDENCE_FIELD_INVALID")
    for key in ("timeout_milliseconds", "observed_duration_milliseconds"):
        value = fields[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WslHangFailureInjectionError("WSL_HANG_EVIDENCE_FIELD_INVALID")
    for key in required - {
        "incident_hash",
        "probe_identity_hash",
        "timeout_milliseconds",
        "observed_duration_milliseconds",
    }:
        if not isinstance(fields[key], bool):
            raise WslHangFailureInjectionError("WSL_HANG_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
