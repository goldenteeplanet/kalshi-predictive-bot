from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

PROHIBITION_SCHEMA_VERSION = "phase4jj-test-host-restart-prohibition-v1"
ProhibitionStatus = Literal["CLEAR", "PROHIBITED", "DENIED", "TAMPERED"]


class HostTestRestartProhibitionError(ValueError):
    """Stable fail-closed test-host restart prohibition error."""


@dataclass(frozen=True)
class TestHostEvidence:
    host_identity_hash: str
    process_evidence_hash: str
    environment_evidence_hash: str
    lock_evidence_hash: str
    test_process_detected: bool
    test_environment_detected: bool
    test_lock_present: bool
    evidence_complete: bool
    integrity_verified: bool
    evidence_hash: str


@dataclass(frozen=True)
class TestHostProhibitionDecision:
    status: ProhibitionStatus
    reasons: tuple[str, ...]
    host_identity_hash: str
    evidence_hash: str
    marker_count: int
    decision_hash: str
    read_only: bool = True
    test_host_clear: bool = False
    restart_prohibited: bool = True
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_test_host_evidence(**fields: Any) -> TestHostEvidence:
    _validate_fields(fields)
    return TestHostEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_test_host_restart_prohibition(evidence: Any) -> TestHostProhibitionDecision:
    item = _validated_evidence(evidence)
    markers = []
    if item.test_process_detected:
        markers.append("TEST_PROCESS_DETECTED")
    if item.test_environment_detected:
        markers.append("TEST_ENVIRONMENT_DETECTED")
    if item.test_lock_present:
        markers.append("TEST_LOCK_PRESENT")
    if not item.evidence_complete or not item.integrity_verified:
        status: ProhibitionStatus = "DENIED"
        reasons = ["TEST_HOST_EVIDENCE_UNTRUSTED"]
    elif markers:
        status = "PROHIBITED"
        reasons = markers
    else:
        status = "CLEAR"
        reasons = []
    clear = status == "CLEAR"
    unsigned = {
        "schema_version": PROHIBITION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "host_identity_hash": item.host_identity_hash,
        "evidence_hash": item.evidence_hash,
        "marker_count": len(markers),
        "read_only": True,
        "test_host_clear": clear,
        "restart_prohibited": not clear,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return TestHostProhibitionDecision(
        status=status,
        reasons=tuple(reasons),
        host_identity_hash=item.host_identity_hash,
        evidence_hash=item.evidence_hash,
        marker_count=len(markers),
        decision_hash=_hash(unsigned),
        test_host_clear=clear,
        restart_prohibited=not clear,
    )


def validate_test_host_prohibition_decision(value: Any) -> None:
    if not isinstance(value, TestHostProhibitionDecision):
        raise HostTestRestartProhibitionError("TEST_HOST_DECISION_TYPE_INVALID")
    clear = value.status == "CLEAR"
    if (
        value.read_only is not True
        or value.test_host_clear != clear
        or value.restart_prohibited != (not clear)
        or any(
            (
                value.restart_authorized,
                value.process_spawn_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise HostTestRestartProhibitionError("TEST_HOST_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = PROHIBITION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise HostTestRestartProhibitionError("TEST_HOST_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> TestHostEvidence:
    if not isinstance(value, TestHostEvidence):
        raise HostTestRestartProhibitionError("TEST_HOST_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise HostTestRestartProhibitionError("TEST_HOST_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "host_identity_hash",
        "process_evidence_hash",
        "environment_evidence_hash",
        "lock_evidence_hash",
        "test_process_detected",
        "test_environment_detected",
        "test_lock_present",
        "evidence_complete",
        "integrity_verified",
    }
    if set(fields) != required:
        raise HostTestRestartProhibitionError("TEST_HOST_EVIDENCE_FIELD_INVALID")
    for key in (
        "host_identity_hash",
        "process_evidence_hash",
        "environment_evidence_hash",
        "lock_evidence_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise HostTestRestartProhibitionError("TEST_HOST_EVIDENCE_FIELD_INVALID")
    for key in (
        "test_process_detected",
        "test_environment_detected",
        "test_lock_present",
        "evidence_complete",
        "integrity_verified",
    ):
        if not isinstance(fields[key], bool):
            raise HostTestRestartProhibitionError("TEST_HOST_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
