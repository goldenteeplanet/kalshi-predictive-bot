from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

REFUSAL_SCHEMA_VERSION = "phase4hz-partial-evidence-refusal-v1"
REQUIRED_EVIDENCE_TYPES = frozenset(
    {"DEPENDENCY_ALLOWLIST", "FAILURE_QUORUM", "FAILURE_PERSISTENCE", "FAILURE_CLASSIFICATION"}
)
EvidenceStatus = Literal["COMPLETE", "REFUSED", "INCOMPLETE", "TAMPERED"]


class PartialEvidenceRefusalError(ValueError):
    """Stable fail-closed partial evidence refusal error."""


@dataclass(frozen=True)
class FailureEvidenceReference:
    evidence_type: str
    artifact_hash: str
    verified: bool
    complete: bool
    reference_hash: str


@dataclass(frozen=True)
class FailureEvidenceEnvelopeDecision:
    status: EvidenceStatus
    reasons: tuple[str, ...]
    evidence_count: int
    evidence_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    complete_evidence_proven: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_failure_evidence_reference(
    *, evidence_type: str, artifact_hash: str, verified: bool, complete: bool
) -> FailureEvidenceReference:
    unsigned = {
        "evidence_type": evidence_type,
        "artifact_hash": artifact_hash,
        "verified": verified,
        "complete": complete,
    }
    _validate_fields(unsigned)
    return FailureEvidenceReference(**unsigned, reference_hash=_hash(unsigned))


def evaluate_partial_evidence_refusal(
    references: Sequence[Any], *, max_records: int = 4
) -> FailureEvidenceEnvelopeDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise PartialEvidenceRefusalError("EVIDENCE_BOUND_INVALID")
    if isinstance(references, (str, bytes)) or len(references) > max_records:
        raise PartialEvidenceRefusalError("EVIDENCE_RECORD_BOUND_EXCEEDED")
    records = [_validated_reference(item) for item in references]
    types = [item.evidence_type for item in records]
    duplicates = len(set(types)) != len(types)
    unknown = sorted(set(types) - REQUIRED_EVIDENCE_TYPES)
    missing = sorted(REQUIRED_EVIDENCE_TYPES - set(types))
    incomplete = sorted(item.evidence_type for item in records if not item.complete)
    unverified = sorted(item.evidence_type for item in records if not item.verified)
    if duplicates or unknown:
        status: EvidenceStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("FAILURE_EVIDENCE_TYPE_DUPLICATE")
        reasons.extend(f"FAILURE_EVIDENCE_TYPE_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"FAILURE_EVIDENCE_MISSING:{item}" for item in missing)]
        reasons.extend(f"FAILURE_EVIDENCE_INCOMPLETE:{item}" for item in incomplete)
    elif unverified:
        status = "REFUSED"
        reasons = [f"FAILURE_EVIDENCE_UNVERIFIED:{item}" for item in unverified]
    else:
        status = "COMPLETE"
        reasons = []
    ordered = sorted(records, key=lambda item: item.evidence_type)
    pairs = tuple((item.evidence_type, item.artifact_hash) for item in ordered)
    set_hash = _hash([asdict(item) for item in ordered])
    proven = status == "COMPLETE"
    unsigned = {
        "schema_version": REFUSAL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_count": len(records),
        "evidence_hashes": [list(item) for item in pairs],
        "evidence_set_hash": set_hash,
        "read_only": True,
        "complete_evidence_proven": proven,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return FailureEvidenceEnvelopeDecision(
        status=status,
        reasons=tuple(reasons),
        evidence_count=len(records),
        evidence_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        complete_evidence_proven=proven,
    )


def validate_failure_evidence_envelope_decision(value: Any) -> None:
    if not isinstance(value, FailureEvidenceEnvelopeDecision):
        raise PartialEvidenceRefusalError("EVIDENCE_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise PartialEvidenceRefusalError("EVIDENCE_SAFETY_BOUNDARY_INVALID")
    if value.complete_evidence_proven != (value.status == "COMPLETE"):
        raise PartialEvidenceRefusalError("EVIDENCE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = REFUSAL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["evidence_hashes"] = [list(item) for item in unsigned["evidence_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise PartialEvidenceRefusalError("EVIDENCE_DECISION_HASH_MISMATCH")


def _validated_reference(value: Any) -> FailureEvidenceReference:
    if not isinstance(value, FailureEvidenceReference):
        raise PartialEvidenceRefusalError("EVIDENCE_REFERENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("reference_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise PartialEvidenceRefusalError("EVIDENCE_REFERENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    if (
        not isinstance(fields["evidence_type"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["evidence_type"]) is None
    ):
        raise PartialEvidenceRefusalError("EVIDENCE_REFERENCE_FIELD_INVALID")
    if (
        not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise PartialEvidenceRefusalError("EVIDENCE_REFERENCE_FIELD_INVALID")
    if not isinstance(fields["verified"], bool) or not isinstance(fields["complete"], bool):
        raise PartialEvidenceRefusalError("EVIDENCE_REFERENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
