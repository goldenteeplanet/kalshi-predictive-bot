from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

TOKEN_SCHEMA_VERSION = "phase4jc-restart-cancellation-token-v1"
MAX_TOKEN_TTL_SECONDS = 300
TokenStatus = Literal["VALID", "USED", "EXPIRED", "DENIED", "INCOMPLETE", "TAMPERED"]


class RestartCancellationTokenError(ValueError):
    """Stable fail-closed restart cancellation token error."""


@dataclass(frozen=True)
class RestartCancellationToken:
    token_id_hash: str
    incident_id_hash: str
    warning_decision_hash: str
    restart_intent_hash: str
    issued_at_epoch: int
    expires_at_epoch: int
    single_use: bool
    consumed: bool
    complete: bool
    token_hash: str


@dataclass(frozen=True)
class RestartCancellationTokenDecision:
    status: TokenStatus
    reasons: tuple[str, ...]
    token_id_hash: str
    incident_id_hash: str
    warning_decision_hash: str
    restart_intent_hash: str
    evaluated_at_epoch: int
    token_hash: str
    decision_hash: str
    read_only: bool = True
    cancellation_validated: bool = False
    restart_denied_after_cancellation: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_cancellation_token(**fields: Any) -> RestartCancellationToken:
    _validate_fields(fields)
    return RestartCancellationToken(**fields, token_hash=_hash(fields))


def evaluate_restart_cancellation_token(
    token: Any,
    *,
    evaluated_at_epoch: int,
    expected_incident_id_hash: str,
    expected_warning_decision_hash: str,
    expected_restart_intent_hash: str,
) -> RestartCancellationTokenDecision:
    if (
        isinstance(evaluated_at_epoch, bool)
        or not isinstance(evaluated_at_epoch, int)
        or evaluated_at_epoch < 0
    ):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_TIME_INVALID")
    for value in (
        expected_incident_id_hash,
        expected_warning_decision_hash,
        expected_restart_intent_hash,
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise RestartCancellationTokenError("RESTART_CANCELLATION_BINDING_INVALID")
    item = _validated_token(token)
    mismatches = []
    if item.incident_id_hash != expected_incident_id_hash:
        mismatches.append("RESTART_CANCELLATION_INCIDENT_MISMATCH")
    if item.warning_decision_hash != expected_warning_decision_hash:
        mismatches.append("RESTART_CANCELLATION_WARNING_MISMATCH")
    if item.restart_intent_hash != expected_restart_intent_hash:
        mismatches.append("RESTART_CANCELLATION_INTENT_MISMATCH")
    ttl = item.expires_at_epoch - item.issued_at_epoch
    if mismatches:
        status: TokenStatus = "TAMPERED"
        reasons = mismatches
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["RESTART_CANCELLATION_TOKEN_INCOMPLETE"]
    elif not item.single_use or ttl < 0 or ttl > MAX_TOKEN_TTL_SECONDS:
        status = "DENIED"
        reasons = ["RESTART_CANCELLATION_TOKEN_POLICY_INVALID"]
    elif evaluated_at_epoch < item.issued_at_epoch:
        status = "DENIED"
        reasons = ["RESTART_CANCELLATION_TOKEN_FROM_FUTURE"]
    elif evaluated_at_epoch > item.expires_at_epoch:
        status = "EXPIRED"
        reasons = ["RESTART_CANCELLATION_TOKEN_EXPIRED"]
    elif item.consumed:
        status = "USED"
        reasons = ["RESTART_CANCELLATION_TOKEN_ALREADY_USED"]
    else:
        status = "VALID"
        reasons = []
    validated = status in {"VALID", "USED"}
    unsigned = {
        "schema_version": TOKEN_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "token_id_hash": item.token_id_hash,
        "incident_id_hash": item.incident_id_hash,
        "warning_decision_hash": item.warning_decision_hash,
        "restart_intent_hash": item.restart_intent_hash,
        "evaluated_at_epoch": evaluated_at_epoch,
        "token_hash": item.token_hash,
        "read_only": True,
        "cancellation_validated": validated,
        "restart_denied_after_cancellation": True,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartCancellationTokenDecision(
        status=status,
        reasons=tuple(reasons),
        token_id_hash=item.token_id_hash,
        incident_id_hash=item.incident_id_hash,
        warning_decision_hash=item.warning_decision_hash,
        restart_intent_hash=item.restart_intent_hash,
        evaluated_at_epoch=evaluated_at_epoch,
        token_hash=item.token_hash,
        decision_hash=_hash(unsigned),
        cancellation_validated=validated,
    )


def validate_restart_cancellation_token_decision(value: Any) -> None:
    if not isinstance(value, RestartCancellationTokenDecision):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.restart_denied_after_cancellation is not True
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_SAFETY_BOUNDARY_INVALID")
    if value.cancellation_validated != (value.status in {"VALID", "USED"}):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = TOKEN_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_DECISION_HASH_MISMATCH")


def _validated_token(value: Any) -> RestartCancellationToken:
    if not isinstance(value, RestartCancellationToken):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_TOKEN_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("token_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RestartCancellationTokenError("RESTART_CANCELLATION_TOKEN_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "token_id_hash",
        "incident_id_hash",
        "warning_decision_hash",
        "restart_intent_hash",
        "issued_at_epoch",
        "expires_at_epoch",
        "single_use",
        "consumed",
        "complete",
    }
    if set(fields) != required:
        raise RestartCancellationTokenError("RESTART_CANCELLATION_TOKEN_FIELD_INVALID")
    for key in (
        "token_id_hash",
        "incident_id_hash",
        "warning_decision_hash",
        "restart_intent_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise RestartCancellationTokenError("RESTART_CANCELLATION_TOKEN_FIELD_INVALID")
    for key in ("issued_at_epoch", "expires_at_epoch"):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise RestartCancellationTokenError("RESTART_CANCELLATION_TOKEN_FIELD_INVALID")
    for key in ("single_use", "consumed", "complete"):
        if not isinstance(fields[key], bool):
            raise RestartCancellationTokenError("RESTART_CANCELLATION_TOKEN_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
