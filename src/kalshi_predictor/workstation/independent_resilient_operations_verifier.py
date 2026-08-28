from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import asdict, dataclass
from typing import Any, Literal

VERIFIER_SCHEMA_VERSION = "phase4kt-independent-resilient-operations-verifier-v1"
REQUIRED_PHASES = tuple(f"4H{letter}" for letter in string.ascii_uppercase[1:]) + tuple(
    f"4{group}{letter}"
    for group, last in (("I", "Z"), ("J", "Z"), ("K", "S"))
    for letter in string.ascii_uppercase[: string.ascii_uppercase.index(last) + 1]
)
VerifierStatus = Literal["CERTIFIED", "DENIED", "INCOMPLETE", "TAMPERED"]


class IndependentResilientOperationsVerifierError(ValueError):
    """Stable fail-closed independent verifier error."""


@dataclass(frozen=True)
class PhaseSafetyEvidence:
    phase: str
    artifact_hash: str
    tests_passed: int
    safety_verified: bool
    independent_review: bool
    complete: bool


@dataclass(frozen=True)
class IndependentVerificationResult:
    status: VerifierStatus
    reasons: tuple[str, ...]
    covered_phases: tuple[str, ...]
    evidence_bundle_hash: str
    total_tests_passed: int
    result_hash: str
    independent_verification_proven: bool = False
    paper_only_proven: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    trading_authorized: bool = False
    execution_authorized: bool = False


def make_phase_safety_evidence(
    *,
    phase: str,
    artifact_hash: str,
    tests_passed: int,
    safety_verified: bool,
    independent_review: bool,
    complete: bool,
) -> PhaseSafetyEvidence:
    if (
        phase not in REQUIRED_PHASES
        or not isinstance(artifact_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", artifact_hash) is None
        or isinstance(tests_passed, bool)
        or not isinstance(tests_passed, int)
        or tests_passed < 0
        or any(
            not isinstance(value, bool) for value in (safety_verified, independent_review, complete)
        )
    ):
        raise IndependentResilientOperationsVerifierError("INDEPENDENT_EVIDENCE_FIELD_INVALID")
    return PhaseSafetyEvidence(
        phase, artifact_hash, tests_passed, safety_verified, independent_review, complete
    )


def evaluate_independent_resilient_operations_verifier(
    records: Any,
) -> IndependentVerificationResult:
    if not isinstance(records, tuple) or any(
        not isinstance(item, PhaseSafetyEvidence) for item in records
    ):
        raise IndependentResilientOperationsVerifierError("INDEPENDENT_EVIDENCE_TYPE_INVALID")
    phases = tuple(item.phase for item in records)
    duplicates = len(set(phases)) != len(phases)
    if duplicates:
        status: VerifierStatus = "TAMPERED"
        reasons = ["INDEPENDENT_PHASE_EVIDENCE_DUPLICATED"]
    elif phases != REQUIRED_PHASES:
        status = "INCOMPLETE"
        reasons = ["INDEPENDENT_PHASE_COVERAGE_INCOMPLETE_OR_UNORDERED"]
    else:
        failures = []
        if any(not item.complete for item in records):
            failures.append("INDEPENDENT_EVIDENCE_INCOMPLETE")
        if any(item.tests_passed <= 0 for item in records):
            failures.append("INDEPENDENT_TEST_EVIDENCE_MISSING")
        if any(not item.safety_verified for item in records):
            failures.append("INDEPENDENT_SAFETY_VERIFICATION_FAILED")
        if any(not item.independent_review for item in records):
            failures.append("INDEPENDENT_REVIEW_MISSING")
        status = "DENIED" if failures else "CERTIFIED"
        reasons = failures
    certified = status == "CERTIFIED"
    payload = [asdict(item) for item in records]
    unsigned = {
        "schema_version": VERIFIER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "covered_phases": list(phases),
        "evidence_bundle_hash": _hash(payload),
        "total_tests_passed": sum(item.tests_passed for item in records),
        "independent_verification_proven": certified,
        "paper_only_proven": certified,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "trading_authorized": False,
        "execution_authorized": False,
    }
    return IndependentVerificationResult(
        status=status,
        reasons=tuple(reasons),
        covered_phases=phases,
        evidence_bundle_hash=unsigned["evidence_bundle_hash"],
        total_tests_passed=unsigned["total_tests_passed"],
        result_hash=_hash(unsigned),
        independent_verification_proven=certified,
        paper_only_proven=certified,
    )


def validate_independent_verification_result(value: Any) -> None:
    if not isinstance(value, IndependentVerificationResult):
        raise IndependentResilientOperationsVerifierError("INDEPENDENT_RESULT_TYPE_INVALID")
    certified = value.status == "CERTIFIED"
    if (
        value.independent_verification_proven != certified
        or value.paper_only_proven != certified
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
        raise IndependentResilientOperationsVerifierError("INDEPENDENT_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = VERIFIER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["covered_phases"] = list(unsigned["covered_phases"])
    if value.result_hash != _hash(unsigned):
        raise IndependentResilientOperationsVerifierError("INDEPENDENT_RESULT_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
