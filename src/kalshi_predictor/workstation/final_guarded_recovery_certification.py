from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

SCHEMA_VERSION = "phase4kw-final-guarded-recovery-certification-v1"
Status = Literal["CERTIFIED_GUARDED", "DENIED", "INCOMPLETE"]


class FinalGuardedRecoveryCertificationError(ValueError):
    """Stable fail-closed final certification error."""


@dataclass(frozen=True)
class FinalCertificationEvidence:
    release_candidate_hash: str
    phase_ledger_hash: str
    rollback_package_hash: str
    covered_phase_count: int
    cumulative_tests_passed: int
    release_candidate_ready: bool
    all_phase_commits_verified: bool
    protected_invariants_verified: bool
    sole_writer_verified: bool
    wsl_running_observed: bool
    scheduler_active_observed: bool
    supervisor_activated: bool
    local_alerting_activated: bool
    restart_adapter_activated: bool
    rollback_verified: bool
    unrelated_changes_preserved: bool
    pushed_remote: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class FinalCertificationResult:
    status: Status
    reasons: tuple[str, ...]
    evidence_hash: str
    covered_phase_count: int
    cumulative_tests_passed: int
    result_hash: str
    guarded_recovery_certified: bool = False
    activation_state: str = "NOT_ACTIVATED"
    activation_requires_separate_authorization: bool = True
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    trading_authorized: bool = False
    execution_authorized: bool = False


def make_final_certification_evidence(**fields: Any) -> FinalCertificationEvidence:
    _validate_fields(fields)
    return FinalCertificationEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_final_guarded_recovery_certification(evidence: Any) -> FinalCertificationResult:
    item = _validated(evidence)
    if not item.complete or item.covered_phase_count != 99:
        status: Status = "INCOMPLETE"
        reasons = ["FINAL_CERTIFICATION_EVIDENCE_OR_PHASE_COVERAGE_INCOMPLETE"]
    else:
        failures = []
        required = {
            "RELEASE_CANDIDATE": item.release_candidate_ready,
            "PHASE_COMMITS": item.all_phase_commits_verified,
            "PROTECTED_INVARIANTS": item.protected_invariants_verified,
            "SOLE_WRITER": item.sole_writer_verified,
            "WSL_RUNNING": item.wsl_running_observed,
            "SCHEDULER_ACTIVE": item.scheduler_active_observed,
            "ROLLBACK": item.rollback_verified,
            "UNRELATED_CHANGES_PRESERVED": item.unrelated_changes_preserved,
        }
        failures.extend(f"FINAL_{name}_FAILED" for name, passed in required.items() if not passed)
        if item.cumulative_tests_passed < 704:
            failures.append("FINAL_TEST_EVIDENCE_INSUFFICIENT")
        if any(
            (
                item.supervisor_activated,
                item.local_alerting_activated,
                item.restart_adapter_activated,
            )
        ):
            failures.append("FINAL_UNAUTHORIZED_ACTIVATION_OBSERVED")
        if item.pushed_remote:
            failures.append("FINAL_UNAUTHORIZED_PUSH_OBSERVED")
        status = "DENIED" if failures else "CERTIFIED_GUARDED"
        reasons = failures
    certified = status == "CERTIFIED_GUARDED"
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "covered_phase_count": item.covered_phase_count,
        "cumulative_tests_passed": item.cumulative_tests_passed,
        "guarded_recovery_certified": certified,
        "activation_state": "NOT_ACTIVATED",
        "activation_requires_separate_authorization": True,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "trading_authorized": False,
        "execution_authorized": False,
    }
    return FinalCertificationResult(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        covered_phase_count=item.covered_phase_count,
        cumulative_tests_passed=item.cumulative_tests_passed,
        result_hash=_hash(unsigned),
        guarded_recovery_certified=certified,
    )


def validate_final_certification_result(value: Any) -> None:
    if not isinstance(value, FinalCertificationResult):
        raise FinalGuardedRecoveryCertificationError("FINAL_CERTIFICATION_RESULT_TYPE_INVALID")
    certified = value.status == "CERTIFIED_GUARDED"
    if (
        value.guarded_recovery_certified != certified
        or value.activation_state != "NOT_ACTIVATED"
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
        raise FinalGuardedRecoveryCertificationError("FINAL_CERTIFICATION_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise FinalGuardedRecoveryCertificationError("FINAL_CERTIFICATION_RESULT_HASH_MISMATCH")


def _validated(value: Any) -> FinalCertificationEvidence:
    if not isinstance(value, FinalCertificationEvidence):
        raise FinalGuardedRecoveryCertificationError("FINAL_CERTIFICATION_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise FinalGuardedRecoveryCertificationError("FINAL_CERTIFICATION_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    hashes = {"release_candidate_hash", "phase_ledger_hash", "rollback_package_hash"}
    integers = {"covered_phase_count", "cumulative_tests_passed"}
    required = (
        hashes
        | integers
        | {
            "release_candidate_ready",
            "all_phase_commits_verified",
            "protected_invariants_verified",
            "sole_writer_verified",
            "wsl_running_observed",
            "scheduler_active_observed",
            "supervisor_activated",
            "local_alerting_activated",
            "restart_adapter_activated",
            "rollback_verified",
            "unrelated_changes_preserved",
            "pushed_remote",
            "complete",
        }
    )
    if set(fields) != required:
        raise FinalGuardedRecoveryCertificationError("FINAL_CERTIFICATION_EVIDENCE_FIELD_INVALID")
    for key in hashes:
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise FinalGuardedRecoveryCertificationError(
                "FINAL_CERTIFICATION_EVIDENCE_FIELD_INVALID"
            )
    for key in integers:
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise FinalGuardedRecoveryCertificationError(
                "FINAL_CERTIFICATION_EVIDENCE_FIELD_INVALID"
            )
    for key in required - hashes - integers:
        if not isinstance(fields[key], bool):
            raise FinalGuardedRecoveryCertificationError(
                "FINAL_CERTIFICATION_EVIDENCE_FIELD_INVALID"
            )


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
