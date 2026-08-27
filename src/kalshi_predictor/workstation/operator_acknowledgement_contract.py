from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .alert_rate_limit_storm_control import (
    AlertAdmissionDecision,
    validate_alert_admission_decision,
)

CONTRACT_SCHEMA_VERSION = "phase4hr-operator-acknowledgement-contract-v1"
AcknowledgementAction = Literal["ACKNOWLEDGED", "DECLINED", "REQUESTED_DIAGNOSTICS"]
AcknowledgementStatus = Literal[
    "ACCEPTED",
    "DECLINED",
    "DIAGNOSTICS_REQUESTED",
    "STALE",
    "INCOMPLETE",
    "TAMPERED",
    "DENIED",
]


class OperatorAcknowledgementContractError(ValueError):
    """Stable fail-closed operator acknowledgement contract error."""


@dataclass(frozen=True)
class OperatorAcknowledgementRecord:
    acknowledgement_id: str
    incident_id_hash: str
    alert_admission_hash: str
    issued_at_epoch_seconds: int
    acknowledged_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    action: AcknowledgementAction
    operator_identity_hash: str
    complete: bool
    acknowledgement_hash: str


@dataclass(frozen=True)
class OperatorAcknowledgementResult:
    status: AcknowledgementStatus
    reasons: tuple[str, ...]
    acknowledgement_id_hash: str
    incident_id_hash: str
    alert_admission_hash: str
    acknowledgement_hash: str
    action: AcknowledgementAction
    operator_identity_hash: str
    issued_at_epoch_seconds: int
    acknowledged_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    evaluated_at_epoch_seconds: int
    max_ttl_seconds: int
    result_hash: str
    read_only: bool = True
    acknowledgement_integrity_proven: bool = False
    operator_acknowledged: bool = False
    diagnostics_requested: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_operator_acknowledgement_record(
    *,
    acknowledgement_id: str,
    incident_id_hash: str,
    alert_admission_hash: str,
    issued_at_epoch_seconds: int,
    acknowledged_at_epoch_seconds: int,
    expires_at_epoch_seconds: int,
    action: AcknowledgementAction,
    operator_identity_hash: str,
    complete: bool,
) -> OperatorAcknowledgementRecord:
    unsigned = {
        "acknowledgement_id": acknowledgement_id,
        "incident_id_hash": incident_id_hash,
        "alert_admission_hash": alert_admission_hash,
        "issued_at_epoch_seconds": issued_at_epoch_seconds,
        "acknowledged_at_epoch_seconds": acknowledged_at_epoch_seconds,
        "expires_at_epoch_seconds": expires_at_epoch_seconds,
        "action": action,
        "operator_identity_hash": operator_identity_hash,
        "complete": complete,
    }
    _validate_record_fields(unsigned)
    return OperatorAcknowledgementRecord(**unsigned, acknowledgement_hash=_hash(unsigned))


def evaluate_operator_acknowledgement(
    alert_admission: Any,
    acknowledgement: Any,
    *,
    expected_incident_id_hash: str,
    evaluated_at_epoch_seconds: int,
    max_ttl_seconds: int = 3_600,
) -> OperatorAcknowledgementResult:
    if not _is_sha256(expected_incident_id_hash):
        raise OperatorAcknowledgementContractError("EXPECTED_INCIDENT_HASH_INVALID")
    for value in (evaluated_at_epoch_seconds, max_ttl_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise OperatorAcknowledgementContractError("CONTRACT_BOUND_INVALID")
    if not isinstance(alert_admission, AlertAdmissionDecision):
        raise OperatorAcknowledgementContractError("ALERT_ADMISSION_TYPE_INVALID")
    try:
        validate_alert_admission_decision(alert_admission)
    except ValueError as exc:
        raise OperatorAcknowledgementContractError("ALERT_ADMISSION_INVALID") from exc
    item = _validated_record(acknowledgement)
    admission_bound = item.alert_admission_hash == alert_admission.decision_hash
    incident_bound = item.incident_id_hash == expected_incident_id_hash
    ttl = item.expires_at_epoch_seconds - item.issued_at_epoch_seconds

    if not item.complete:
        status: AcknowledgementStatus = "INCOMPLETE"
        reasons = ["OPERATOR_ACKNOWLEDGEMENT_INCOMPLETE"]
    elif not admission_bound or not incident_bound:
        status = "TAMPERED"
        reasons = []
        if not admission_bound:
            reasons.append("ACKNOWLEDGEMENT_ALERT_BINDING_MISMATCH")
        if not incident_bound:
            reasons.append("ACKNOWLEDGEMENT_INCIDENT_BINDING_MISMATCH")
    elif alert_admission.status != "ALLOW":
        status = "DENIED"
        reasons = [f"ALERT_ADMISSION_NOT_ALLOWED:{alert_admission.status}"]
    elif item.acknowledged_at_epoch_seconds < item.issued_at_epoch_seconds:
        status = "DENIED"
        reasons = ["ACKNOWLEDGEMENT_BEFORE_ISSUE"]
    elif item.acknowledged_at_epoch_seconds > evaluated_at_epoch_seconds:
        status = "DENIED"
        reasons = ["ACKNOWLEDGEMENT_FROM_FUTURE"]
    elif ttl < 0 or ttl > max_ttl_seconds:
        status = "DENIED"
        reasons = ["ACKNOWLEDGEMENT_TTL_INVALID"]
    elif evaluated_at_epoch_seconds > item.expires_at_epoch_seconds:
        status = "STALE"
        reasons = ["ACKNOWLEDGEMENT_EXPIRED"]
    elif item.action == "ACKNOWLEDGED":
        status = "ACCEPTED"
        reasons = []
    elif item.action == "DECLINED":
        status = "DECLINED"
        reasons = []
    else:
        status = "DIAGNOSTICS_REQUESTED"
        reasons = []

    acknowledged = status == "ACCEPTED"
    diagnostics = status == "DIAGNOSTICS_REQUESTED"
    unsigned = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "acknowledgement_id_hash": _hash(item.acknowledgement_id),
        "incident_id_hash": item.incident_id_hash,
        "alert_admission_hash": alert_admission.decision_hash,
        "acknowledgement_hash": item.acknowledgement_hash,
        "action": item.action,
        "operator_identity_hash": item.operator_identity_hash,
        "issued_at_epoch_seconds": item.issued_at_epoch_seconds,
        "acknowledged_at_epoch_seconds": item.acknowledged_at_epoch_seconds,
        "expires_at_epoch_seconds": item.expires_at_epoch_seconds,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "max_ttl_seconds": max_ttl_seconds,
        "read_only": True,
        "acknowledgement_integrity_proven": True,
        "operator_acknowledged": acknowledged,
        "diagnostics_requested": diagnostics,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return OperatorAcknowledgementResult(
        status=status,
        reasons=tuple(reasons),
        acknowledgement_id_hash=unsigned["acknowledgement_id_hash"],
        incident_id_hash=item.incident_id_hash,
        alert_admission_hash=alert_admission.decision_hash,
        acknowledgement_hash=item.acknowledgement_hash,
        action=item.action,
        operator_identity_hash=item.operator_identity_hash,
        issued_at_epoch_seconds=item.issued_at_epoch_seconds,
        acknowledged_at_epoch_seconds=item.acknowledged_at_epoch_seconds,
        expires_at_epoch_seconds=item.expires_at_epoch_seconds,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        max_ttl_seconds=max_ttl_seconds,
        result_hash=_hash(unsigned),
        acknowledgement_integrity_proven=True,
        operator_acknowledged=acknowledged,
        diagnostics_requested=diagnostics,
    )


def validate_operator_acknowledgement_result(result: Any) -> None:
    if not isinstance(result, OperatorAcknowledgementResult):
        raise OperatorAcknowledgementContractError("RESULT_TYPE_INVALID")
    if result.read_only is not True or any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise OperatorAcknowledgementContractError("RESULT_SAFETY_BOUNDARY_INVALID")
    if result.status == "ACCEPTED" and not result.operator_acknowledged:
        raise OperatorAcknowledgementContractError("RESULT_STATUS_INVALID")
    if result.status == "DIAGNOSTICS_REQUESTED" and not result.diagnostics_requested:
        raise OperatorAcknowledgementContractError("RESULT_STATUS_INVALID")
    unsigned = asdict(result)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = CONTRACT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.result_hash != _hash(unsigned):
        raise OperatorAcknowledgementContractError("RESULT_HASH_MISMATCH")


def _validated_record(value: Any) -> OperatorAcknowledgementRecord:
    if not isinstance(value, OperatorAcknowledgementRecord):
        raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("acknowledgement_hash")
    _validate_record_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_HASH_MISMATCH")
    return value


def _validate_record_fields(payload: dict[str, Any]) -> None:
    if payload["action"] not in {"ACKNOWLEDGED", "DECLINED", "REQUESTED_DIAGNOSTICS"}:
        raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_FIELD_INVALID")
    if not isinstance(payload["acknowledgement_id"], str) or not payload[
        "acknowledgement_id"
    ].strip():
        raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_FIELD_INVALID")
    for key in ("incident_id_hash", "alert_admission_hash", "operator_identity_hash"):
        if not _is_sha256(payload[key]):
            raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_FIELD_INVALID")
    for key in (
        "issued_at_epoch_seconds",
        "acknowledged_at_epoch_seconds",
        "expires_at_epoch_seconds",
    ):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise OperatorAcknowledgementContractError("ACKNOWLEDGEMENT_FIELD_INVALID")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
