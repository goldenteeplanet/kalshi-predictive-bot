from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

SCHEMA_VERSION = "phase4kv-resilient-operations-release-candidate-v1"
Status = Literal["READY", "DENIED", "INCOMPLETE"]


class ResilientOperationsReleaseCandidateError(ValueError):
    """Stable fail-closed resilient-operations release-candidate error."""


@dataclass(frozen=True)
class ReleaseCandidateEvidence:
    independent_verification_hash: str
    operator_drill_hash: str
    post_boot_gate_hash: str
    rollback_package_hash: str
    covered_phase_count: int
    cumulative_tests_passed: int
    independent_verification_passed: bool
    operator_drill_passed: bool
    post_boot_workstream_passed: bool
    rollback_verified: bool
    protected_invariants_verified: bool
    paper_only_verified: bool
    unrelated_changes_preserved: bool
    pushed_remote: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class ReleaseCandidateResult:
    status: Status
    reasons: tuple[str, ...]
    evidence_hash: str
    covered_phase_count: int
    cumulative_tests_passed: int
    result_hash: str
    release_candidate_ready: bool = False
    activation_requires_separate_authorization: bool = True
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    trading_authorized: bool = False
    execution_authorized: bool = False


def make_release_candidate_evidence(**fields: Any) -> ReleaseCandidateEvidence:
    _validate_fields(fields)
    return ReleaseCandidateEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_resilient_operations_release_candidate(evidence: Any) -> ReleaseCandidateResult:
    item = _validated(evidence)
    if not item.complete or item.covered_phase_count != 98:
        status: Status = "INCOMPLETE"
        reasons = ["RELEASE_CANDIDATE_EVIDENCE_OR_PHASE_COVERAGE_INCOMPLETE"]
    else:
        failures = []
        checks = {
            "INDEPENDENT_VERIFICATION": item.independent_verification_passed,
            "OPERATOR_DRILL": item.operator_drill_passed,
            "POST_BOOT_WORKSTREAM": item.post_boot_workstream_passed,
            "ROLLBACK": item.rollback_verified,
            "PROTECTED_INVARIANTS": item.protected_invariants_verified,
            "PAPER_ONLY": item.paper_only_verified,
            "UNRELATED_CHANGES_PRESERVED": item.unrelated_changes_preserved,
        }
        failures.extend(
            f"RELEASE_CANDIDATE_{name}_FAILED" for name, passed in checks.items() if not passed
        )
        if item.cumulative_tests_passed < 694:
            failures.append("RELEASE_CANDIDATE_TEST_EVIDENCE_INSUFFICIENT")
        if item.pushed_remote:
            failures.append("RELEASE_CANDIDATE_UNAUTHORIZED_PUSH_OBSERVED")
        status = "DENIED" if failures else "READY"
        reasons = failures
    ready = status == "READY"
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "covered_phase_count": item.covered_phase_count,
        "cumulative_tests_passed": item.cumulative_tests_passed,
        "release_candidate_ready": ready,
        "activation_requires_separate_authorization": True,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "trading_authorized": False,
        "execution_authorized": False,
    }
    return ReleaseCandidateResult(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        covered_phase_count=item.covered_phase_count,
        cumulative_tests_passed=item.cumulative_tests_passed,
        result_hash=_hash(unsigned),
        release_candidate_ready=ready,
    )


def validate_release_candidate_result(value: Any) -> None:
    if not isinstance(value, ReleaseCandidateResult):
        raise ResilientOperationsReleaseCandidateError("RELEASE_CANDIDATE_RESULT_TYPE_INVALID")
    ready = value.status == "READY"
    if (
        value.release_candidate_ready != ready
        or value.activation_requires_separate_authorization is not True
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.trading_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise ResilientOperationsReleaseCandidateError("RELEASE_CANDIDATE_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise ResilientOperationsReleaseCandidateError("RELEASE_CANDIDATE_RESULT_HASH_MISMATCH")


def _validated(value: Any) -> ReleaseCandidateEvidence:
    if not isinstance(value, ReleaseCandidateEvidence):
        raise ResilientOperationsReleaseCandidateError("RELEASE_CANDIDATE_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ResilientOperationsReleaseCandidateError("RELEASE_CANDIDATE_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "independent_verification_hash",
        "operator_drill_hash",
        "post_boot_gate_hash",
        "rollback_package_hash",
        "covered_phase_count",
        "cumulative_tests_passed",
        "independent_verification_passed",
        "operator_drill_passed",
        "post_boot_workstream_passed",
        "rollback_verified",
        "protected_invariants_verified",
        "paper_only_verified",
        "unrelated_changes_preserved",
        "pushed_remote",
        "complete",
    }
    if set(fields) != required:
        raise ResilientOperationsReleaseCandidateError("RELEASE_CANDIDATE_EVIDENCE_FIELD_INVALID")
    for key in (
        "independent_verification_hash",
        "operator_drill_hash",
        "post_boot_gate_hash",
        "rollback_package_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise ResilientOperationsReleaseCandidateError(
                "RELEASE_CANDIDATE_EVIDENCE_FIELD_INVALID"
            )
    for key in ("covered_phase_count", "cumulative_tests_passed"):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise ResilientOperationsReleaseCandidateError(
                "RELEASE_CANDIDATE_EVIDENCE_FIELD_INVALID"
            )
    for key in required - {
        "independent_verification_hash",
        "operator_drill_hash",
        "post_boot_gate_hash",
        "rollback_package_hash",
        "covered_phase_count",
        "cumulative_tests_passed",
    }:
        if not isinstance(fields[key], bool):
            raise ResilientOperationsReleaseCandidateError(
                "RELEASE_CANDIDATE_EVIDENCE_FIELD_INVALID"
            )


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
