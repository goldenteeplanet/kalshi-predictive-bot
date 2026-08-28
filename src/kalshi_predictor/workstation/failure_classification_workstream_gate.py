from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4ie-failure-classification-workstream-gate-v1"
REQUIRED_COMPONENTS = frozenset(
    {
        "CRITICAL_DEPENDENCY_ALLOWLIST",
        "FAILURE_OBSERVATION_QUORUM",
        "FAILURE_PERSISTENCE_WINDOW",
        "UNKNOWN_FAILURE_QUARANTINE",
        "PARTIAL_EVIDENCE_REFUSAL",
        "DATABASE_READABILITY_CLASSIFIER",
        "DISK_EXHAUSTION_CLASSIFIER",
        "CLOCK_SKEW_CLASSIFIER",
        "NETWORK_FAILURE_NON_RESTART_RULE",
    }
)
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]


class FailureClassificationWorkstreamGateError(ValueError):
    """Stable fail-closed failure-classification workstream gate error."""


@dataclass(frozen=True)
class FailureClassificationComponentEvidence:
    component: str
    artifact_hash: str
    verified: bool
    complete: bool
    safety_boundary_proven: bool
    evidence_hash: str


@dataclass(frozen=True)
class FailureClassificationWorkstreamDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    failure_classification_ready: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_failure_classification_component_evidence(
    **fields: Any,
) -> FailureClassificationComponentEvidence:
    _validate_fields(fields)
    return FailureClassificationComponentEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_failure_classification_workstream_gate(
    evidence: Sequence[Any], *, max_records: int = 9
) -> FailureClassificationWorkstreamDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_GATE_BOUND_INVALID")
    if isinstance(evidence, (str, bytes)) or len(evidence) > max_records:
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_GATE_RECORD_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in evidence]
    components = [item.component for item in records]
    duplicates = len(set(components)) != len(components)
    unknown = sorted(set(components) - REQUIRED_COMPONENTS)
    missing = sorted(REQUIRED_COMPONENTS - set(components))
    incomplete = sorted(item.component for item in records if not item.complete)
    unverified = sorted(item.component for item in records if not item.verified)
    unsafe = sorted(item.component for item in records if not item.safety_boundary_proven)
    if duplicates or unknown:
        status: GateStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("CLASSIFICATION_COMPONENT_DUPLICATE")
        reasons.extend(f"CLASSIFICATION_COMPONENT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"CLASSIFICATION_COMPONENT_MISSING:{item}" for item in missing)]
        reasons.extend(f"CLASSIFICATION_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or unsafe:
        status = "NOT_READY"
        reasons = [*(f"CLASSIFICATION_COMPONENT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"CLASSIFICATION_SAFETY_UNPROVEN:{item}" for item in unsafe)
    else:
        status = "READY"
        reasons = []
    ordered = sorted(records, key=lambda item: item.component)
    pairs = tuple((item.component, item.artifact_hash) for item in ordered)
    set_hash = _hash([asdict(item) for item in ordered])
    ready = status == "READY"
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "component_count": len(records),
        "component_hashes": [list(item) for item in pairs],
        "evidence_set_hash": set_hash,
        "read_only": True,
        "failure_classification_ready": ready,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return FailureClassificationWorkstreamDecision(
        status=status,
        reasons=tuple(reasons),
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        failure_classification_ready=ready,
    )


def validate_failure_classification_workstream_decision(value: Any) -> None:
    if not isinstance(value, FailureClassificationWorkstreamDecision):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_GATE_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise FailureClassificationWorkstreamGateError(
            "CLASSIFICATION_GATE_SAFETY_BOUNDARY_INVALID"
        )
    if value.failure_classification_ready != (value.status == "READY"):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_GATE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_GATE_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> FailureClassificationComponentEvidence:
    if not isinstance(value, FailureClassificationComponentEvidence):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_COMPONENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_COMPONENT_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {"component", "artifact_hash", "verified", "complete", "safety_boundary_proven"}
    if (
        set(fields) != required
        or not isinstance(fields["component"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["component"]) is None
    ):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_COMPONENT_FIELD_INVALID")
    if (
        not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise FailureClassificationWorkstreamGateError("CLASSIFICATION_COMPONENT_FIELD_INVALID")
    for key in ("verified", "complete", "safety_boundary_proven"):
        if not isinstance(fields[key], bool):
            raise FailureClassificationWorkstreamGateError("CLASSIFICATION_COMPONENT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
