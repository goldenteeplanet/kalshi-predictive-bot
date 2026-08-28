from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

TAXONOMY_SCHEMA_VERSION = "phase4ja-restart-denial-reason-taxonomy-v1"
DENIAL_PRIORITY = (
    "TEST_HOST",
    "EVIDENCE_TAMPERED",
    "EVIDENCE_INCOMPLETE",
    "UNKNOWN_FAILURE",
    "FAILURE_NOT_ALLOWLISTED",
    "CLASSIFIER_NOT_RESTART_REQUIRED",
    "COMPONENT_RECOVERY_NOT_FAILED",
    "TRADING_NOT_FAIL_CLOSED",
    "PROTECTED_INVARIANT_CHANGED",
    "WRITER_EXCLUSIVITY_UNPROVEN",
    "WARNING_UNPROVEN",
    "CANCELLATION_UNPROVEN",
    "INTENT_NOT_PERSISTED",
    "COOLDOWN_ACTIVE",
    "RESTART_BUDGET_EXHAUSTED",
    "RESTART_LOOP_BREAKER_OPEN",
    "OPERATOR_INTERVENTION_REQUIRED",
)
DENIAL_REASONS = frozenset(DENIAL_PRIORITY)
TaxonomyStatus = Literal["CLASSIFIED", "NOT_DENIED", "TAMPERED"]


class RestartDenialReasonTaxonomyError(ValueError):
    """Stable fail-closed restart denial taxonomy error."""


@dataclass(frozen=True)
class RestartDenialEvidence:
    incident_id_hash: str
    source_decision_hash: str
    source_status: str
    reason_codes: tuple[str, ...]
    evidence_hash: str


@dataclass(frozen=True)
class RestartDenialDecision:
    status: TaxonomyStatus
    reasons: tuple[str, ...]
    primary_reason: str
    incident_id_hash: str
    source_decision_hash: str
    taxonomy_hash: str
    decision_hash: str
    read_only: bool = True
    restart_denied: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_denial_evidence(**fields: Any) -> RestartDenialEvidence:
    normalized = dict(fields)
    if isinstance(normalized.get("reason_codes"), list):
        normalized["reason_codes"] = tuple(normalized["reason_codes"])
    _validate_fields(normalized)
    unsigned = {**normalized, "reason_codes": list(normalized["reason_codes"])}
    return RestartDenialEvidence(**normalized, evidence_hash=_hash(unsigned))


def classify_restart_denial(evidence: Any) -> RestartDenialDecision:
    item = _validated_evidence(evidence)
    supplied = set(item.reason_codes)
    unknown = sorted(supplied - DENIAL_REASONS)
    duplicates = len(supplied) != len(item.reason_codes)
    if unknown or duplicates:
        status: TaxonomyStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("DENIAL_REASON_DUPLICATE")
        reasons.extend(f"DENIAL_REASON_UNKNOWN:{reason}" for reason in unknown)
        primary = "EVIDENCE_TAMPERED"
    elif item.source_status == "ELIGIBLE":
        if supplied:
            status = "TAMPERED"
            reasons = ["ELIGIBLE_DECISION_HAS_DENIAL_REASONS"]
            primary = "EVIDENCE_TAMPERED"
        else:
            status = "NOT_DENIED"
            reasons = []
            primary = "NONE"
    elif not supplied:
        status = "TAMPERED"
        reasons = ["DENIED_DECISION_REASON_MISSING"]
        primary = "EVIDENCE_TAMPERED"
    else:
        status = "CLASSIFIED"
        reasons = [reason for reason in DENIAL_PRIORITY if reason in supplied]
        primary = reasons[0]
    denied = status != "NOT_DENIED"
    taxonomy_hash = _hash(list(DENIAL_PRIORITY))
    unsigned = {
        "schema_version": TAXONOMY_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "primary_reason": primary,
        "incident_id_hash": item.incident_id_hash,
        "source_decision_hash": item.source_decision_hash,
        "taxonomy_hash": taxonomy_hash,
        "read_only": True,
        "restart_denied": denied,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartDenialDecision(
        status=status,
        reasons=tuple(reasons),
        primary_reason=primary,
        incident_id_hash=item.incident_id_hash,
        source_decision_hash=item.source_decision_hash,
        taxonomy_hash=taxonomy_hash,
        decision_hash=_hash(unsigned),
        restart_denied=denied,
    )


def validate_restart_denial_decision(value: Any) -> None:
    if not isinstance(value, RestartDenialDecision):
        raise RestartDenialReasonTaxonomyError("DENIAL_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
    ):
        raise RestartDenialReasonTaxonomyError("DENIAL_SAFETY_BOUNDARY_INVALID")
    if value.restart_denied != (value.status != "NOT_DENIED"):
        raise RestartDenialReasonTaxonomyError("DENIAL_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = TAXONOMY_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise RestartDenialReasonTaxonomyError("DENIAL_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> RestartDenialEvidence:
    if not isinstance(value, RestartDenialEvidence):
        raise RestartDenialReasonTaxonomyError("DENIAL_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    unsigned["reason_codes"] = tuple(unsigned["reason_codes"])
    _validate_fields(unsigned)
    unsigned["reason_codes"] = list(unsigned["reason_codes"])
    if supplied != _hash(unsigned):
        raise RestartDenialReasonTaxonomyError("DENIAL_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {"incident_id_hash", "source_decision_hash", "source_status", "reason_codes"}
    if set(fields) != required:
        raise RestartDenialReasonTaxonomyError("DENIAL_EVIDENCE_FIELD_INVALID")
    for key in ("incident_id_hash", "source_decision_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise RestartDenialReasonTaxonomyError("DENIAL_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["source_status"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["source_status"]) is None
        or not isinstance(fields["reason_codes"], tuple)
        or len(fields["reason_codes"]) > len(DENIAL_REASONS)
        or any(
            not isinstance(item, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item) is None
            for item in fields["reason_codes"]
        )
    ):
        raise RestartDenialReasonTaxonomyError("DENIAL_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
