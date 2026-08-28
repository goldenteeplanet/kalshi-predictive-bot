from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

CLASSIFIER_SCHEMA_VERSION = "phase4ia-database-readability-classifier-v1"
DatabaseStatus = Literal["READABLE", "UNREADABLE", "UNKNOWN", "INCOMPLETE", "TAMPERED"]
KNOWN_DATABASE_ERRORS = frozenset(
    {
        "CONNECTION_REFUSED",
        "FILE_NOT_FOUND",
        "PERMISSION_DENIED",
        "QUERY_FAILED",
        "SCHEMA_UNREADABLE",
    }
)


class DatabaseReadabilityClassifierError(ValueError):
    """Stable fail-closed database readability classifier error."""


@dataclass(frozen=True)
class DatabaseReadabilityEvidence:
    probe_id_hash: str
    observed_at_epoch_seconds: int
    connection_opened: bool
    schema_readable: bool
    protected_query_readable: bool
    error_code: str
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class DatabaseReadabilityDecision:
    status: DatabaseStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    evaluated_at_epoch_seconds: int
    maximum_age_seconds: int
    decision_hash: str
    read_only: bool = True
    database_readability_proven: bool = False
    operator_alert_required: bool = True
    restart_eligible: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_database_readability_evidence(
    *,
    probe_id_hash: str,
    observed_at_epoch_seconds: int,
    connection_opened: bool,
    schema_readable: bool,
    protected_query_readable: bool,
    error_code: str,
    complete: bool,
) -> DatabaseReadabilityEvidence:
    unsigned = {
        "probe_id_hash": probe_id_hash,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "connection_opened": connection_opened,
        "schema_readable": schema_readable,
        "protected_query_readable": protected_query_readable,
        "error_code": error_code,
        "complete": complete,
    }
    _validate_fields(unsigned)
    return DatabaseReadabilityEvidence(**unsigned, evidence_hash=_hash(unsigned))


def classify_database_readability(
    evidence: Any, *, evaluated_at_epoch_seconds: int, maximum_age_seconds: int = 120
) -> DatabaseReadabilityDecision:
    for value in (evaluated_at_epoch_seconds, maximum_age_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DatabaseReadabilityClassifierError("DATABASE_CLASSIFIER_BOUND_INVALID")
    item = _validated_evidence(evidence)
    coherent_success = (
        item.connection_opened
        and item.schema_readable
        and item.protected_query_readable
        and item.error_code == "NONE"
    )
    coherent_failure = (
        not (item.connection_opened and item.schema_readable and item.protected_query_readable)
        and item.error_code in KNOWN_DATABASE_ERRORS
    )
    if item.observed_at_epoch_seconds > evaluated_at_epoch_seconds:
        status: DatabaseStatus = "TAMPERED"
        reasons = ["DATABASE_EVIDENCE_FROM_FUTURE"]
    elif evaluated_at_epoch_seconds - item.observed_at_epoch_seconds > maximum_age_seconds:
        status = "UNKNOWN"
        reasons = ["DATABASE_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["DATABASE_EVIDENCE_INCOMPLETE"]
    elif coherent_success:
        status = "READABLE"
        reasons = []
    elif coherent_failure:
        status = "UNREADABLE"
        reasons = [f"DATABASE_READABILITY_FAILURE:{item.error_code}"]
    else:
        status = "UNKNOWN"
        reasons = ["DATABASE_EVIDENCE_CONTRADICTORY_OR_UNKNOWN"]
    proven = status == "READABLE"
    unsigned = {
        "schema_version": CLASSIFIER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "maximum_age_seconds": maximum_age_seconds,
        "read_only": True,
        "database_readability_proven": proven,
        "operator_alert_required": not proven,
        "restart_eligible": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DatabaseReadabilityDecision(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        maximum_age_seconds=maximum_age_seconds,
        decision_hash=_hash(unsigned),
        database_readability_proven=proven,
        operator_alert_required=not proven,
    )


def validate_database_readability_decision(value: Any) -> None:
    if not isinstance(value, DatabaseReadabilityDecision):
        raise DatabaseReadabilityClassifierError("DATABASE_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.restart_eligible is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise DatabaseReadabilityClassifierError("DATABASE_SAFETY_BOUNDARY_INVALID")
    if value.database_readability_proven != (value.status == "READABLE"):
        raise DatabaseReadabilityClassifierError("DATABASE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = CLASSIFIER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise DatabaseReadabilityClassifierError("DATABASE_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> DatabaseReadabilityEvidence:
    if not isinstance(value, DatabaseReadabilityEvidence):
        raise DatabaseReadabilityClassifierError("DATABASE_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DatabaseReadabilityClassifierError("DATABASE_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    if (
        not isinstance(fields["probe_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["probe_id_hash"]) is None
    ):
        raise DatabaseReadabilityClassifierError("DATABASE_EVIDENCE_FIELD_INVALID")
    timestamp = fields["observed_at_epoch_seconds"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise DatabaseReadabilityClassifierError("DATABASE_EVIDENCE_FIELD_INVALID")
    for key in ("connection_opened", "schema_readable", "protected_query_readable", "complete"):
        if not isinstance(fields[key], bool):
            raise DatabaseReadabilityClassifierError("DATABASE_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["error_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["error_code"]) is None
    ):
        raise DatabaseReadabilityClassifierError("DATABASE_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
