from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .failure_persistence_window import (
    FailurePersistenceDecision,
    validate_failure_persistence_decision,
)

QUARANTINE_SCHEMA_VERSION = "phase4hy-unknown-failure-quarantine-v1"
KNOWN_FAILURE_CLASSES = frozenset(
    {"WSL_VM_UNAVAILABLE", "SYSTEMD_USER_UNAVAILABLE", "SCHEDULER_UNAVAILABLE"}
)
QuarantineStatus = Literal["CLASSIFIED", "QUARANTINED", "NOT_PERSISTENT", "INCOMPLETE", "TAMPERED"]


class UnknownFailureQuarantineError(ValueError):
    """Stable fail-closed unknown failure quarantine error."""


@dataclass(frozen=True)
class FailureClassificationClaim:
    incident_id_hash: str
    persistence_decision_hash: str
    failure_class: str
    classified_at_epoch_seconds: int
    complete: bool
    claim_hash: str


@dataclass(frozen=True)
class UnknownFailureQuarantineDecision:
    status: QuarantineStatus
    reasons: tuple[str, ...]
    incident_id_hash: str
    dependency_code: str
    failure_class: str
    persistence_decision_hash: str
    claim_hash: str
    evaluated_at_epoch_seconds: int
    decision_hash: str
    read_only: bool = True
    failure_classified: bool = False
    quarantined: bool = True
    operator_alert_required: bool = True
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_failure_classification_claim(
    *,
    incident_id_hash: str,
    persistence_decision_hash: str,
    failure_class: str,
    classified_at_epoch_seconds: int,
    complete: bool,
) -> FailureClassificationClaim:
    unsigned = {
        "incident_id_hash": incident_id_hash,
        "persistence_decision_hash": persistence_decision_hash,
        "failure_class": failure_class,
        "classified_at_epoch_seconds": classified_at_epoch_seconds,
        "complete": complete,
    }
    _validate_claim_fields(unsigned)
    return FailureClassificationClaim(**unsigned, claim_hash=_hash(unsigned))


def evaluate_unknown_failure_quarantine(
    persistence: Any,
    claim: Any,
    *,
    evaluated_at_epoch_seconds: int,
) -> UnknownFailureQuarantineDecision:
    if (
        isinstance(evaluated_at_epoch_seconds, bool)
        or not isinstance(evaluated_at_epoch_seconds, int)
        or evaluated_at_epoch_seconds < 0
    ):
        raise UnknownFailureQuarantineError("QUARANTINE_BOUND_INVALID")
    if not isinstance(persistence, FailurePersistenceDecision):
        raise UnknownFailureQuarantineError("PERSISTENCE_TYPE_INVALID")
    try:
        validate_failure_persistence_decision(persistence)
    except ValueError as exc:
        raise UnknownFailureQuarantineError("PERSISTENCE_INVALID") from exc
    item = _validated_claim(claim)
    bound = item.persistence_decision_hash == persistence.decision_hash
    if not bound:
        status: QuarantineStatus = "TAMPERED"
        reasons = ["CLASSIFICATION_PERSISTENCE_BINDING_MISMATCH"]
    elif item.classified_at_epoch_seconds > evaluated_at_epoch_seconds:
        status = "TAMPERED"
        reasons = ["CLASSIFICATION_FROM_FUTURE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["CLASSIFICATION_CLAIM_INCOMPLETE"]
    elif persistence.status != "PERSISTENT":
        status = "NOT_PERSISTENT"
        reasons = [f"FAILURE_NOT_PERSISTENT:{persistence.status}"]
    elif item.failure_class not in KNOWN_FAILURE_CLASSES:
        status = "QUARANTINED"
        reasons = [f"UNKNOWN_FAILURE_CLASS:{item.failure_class}"]
    else:
        status = "CLASSIFIED"
        reasons = []
    classified = status == "CLASSIFIED"
    quarantined = not classified
    unsigned = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "incident_id_hash": item.incident_id_hash,
        "dependency_code": persistence.dependency_code,
        "failure_class": item.failure_class,
        "persistence_decision_hash": persistence.decision_hash,
        "claim_hash": item.claim_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "read_only": True,
        "failure_classified": classified,
        "quarantined": quarantined,
        "operator_alert_required": quarantined,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return UnknownFailureQuarantineDecision(
        status=status,
        reasons=tuple(reasons),
        incident_id_hash=item.incident_id_hash,
        dependency_code=persistence.dependency_code,
        failure_class=item.failure_class,
        persistence_decision_hash=persistence.decision_hash,
        claim_hash=item.claim_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        decision_hash=_hash(unsigned),
        failure_classified=classified,
        quarantined=quarantined,
        operator_alert_required=quarantined,
    )


def validate_unknown_failure_quarantine_decision(value: Any) -> None:
    if not isinstance(value, UnknownFailureQuarantineDecision):
        raise UnknownFailureQuarantineError("QUARANTINE_RESULT_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise UnknownFailureQuarantineError("QUARANTINE_SAFETY_BOUNDARY_INVALID")
    if (
        value.failure_classified != (value.status == "CLASSIFIED")
        or value.quarantined == value.failure_classified
    ):
        raise UnknownFailureQuarantineError("QUARANTINE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = QUARANTINE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise UnknownFailureQuarantineError("QUARANTINE_RESULT_HASH_MISMATCH")


def _validated_claim(value: Any) -> FailureClassificationClaim:
    if not isinstance(value, FailureClassificationClaim):
        raise UnknownFailureQuarantineError("CLASSIFICATION_CLAIM_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("claim_hash")
    _validate_claim_fields(unsigned)
    if supplied != _hash(unsigned):
        raise UnknownFailureQuarantineError("CLASSIFICATION_CLAIM_HASH_MISMATCH")
    return value


def _validate_claim_fields(fields: dict[str, Any]) -> None:
    for key in ("incident_id_hash", "persistence_decision_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise UnknownFailureQuarantineError("CLASSIFICATION_CLAIM_FIELD_INVALID")
    if (
        not isinstance(fields["failure_class"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["failure_class"]) is None
    ):
        raise UnknownFailureQuarantineError("CLASSIFICATION_CLAIM_FIELD_INVALID")
    timestamp = fields["classified_at_epoch_seconds"]
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        or not isinstance(fields["complete"], bool)
    ):
        raise UnknownFailureQuarantineError("CLASSIFICATION_CLAIM_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
