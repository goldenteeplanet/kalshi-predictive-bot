from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

COMMAND_SCHEMA_VERSION = "phase4hs-recovery-cancellation-command-v1"
CancellationStatus = Literal[
    "CANCELLED", "ALREADY_CANCELLED", "STALE", "INCOMPLETE", "TAMPERED", "DENIED"
]


class RecoveryCancellationCommandError(ValueError):
    """Stable fail-closed recovery cancellation command error."""


@dataclass(frozen=True)
class RecoveryAuthorityState:
    incident_id_hash: str
    authorization_hash: str
    active: bool
    issued_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    state_hash: str


@dataclass(frozen=True)
class RecoveryCancellationCommand:
    command_id: str
    incident_id_hash: str
    target_authorization_hash: str
    issued_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    operator_identity_hash: str
    reason_code: str
    complete: bool
    command_hash: str


@dataclass(frozen=True)
class RecoveryCancellationResult:
    status: CancellationStatus
    reasons: tuple[str, ...]
    command_id_hash: str
    command_hash: str
    incident_id_hash: str
    target_authorization_hash: str
    authority_state_hash: str
    operator_identity_hash: str
    evaluated_at_epoch_seconds: int
    max_command_ttl_seconds: int
    authority_active_before: bool
    authority_active_after: bool
    result_hash: str
    read_only: bool = True
    cancellation_validated: bool = False
    cancellation_applied: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_authority_state(
    *,
    incident_id_hash: str,
    authorization_hash: str,
    active: bool,
    issued_at_epoch_seconds: int,
    expires_at_epoch_seconds: int,
) -> RecoveryAuthorityState:
    unsigned = {
        "incident_id_hash": incident_id_hash,
        "authorization_hash": authorization_hash,
        "active": active,
        "issued_at_epoch_seconds": issued_at_epoch_seconds,
        "expires_at_epoch_seconds": expires_at_epoch_seconds,
    }
    _validate_authority_fields(unsigned)
    return RecoveryAuthorityState(**unsigned, state_hash=_hash(unsigned))


def make_recovery_cancellation_command(
    *,
    command_id: str,
    incident_id_hash: str,
    target_authorization_hash: str,
    issued_at_epoch_seconds: int,
    expires_at_epoch_seconds: int,
    operator_identity_hash: str,
    reason_code: str,
    complete: bool,
) -> RecoveryCancellationCommand:
    unsigned = {
        "command_id": command_id,
        "incident_id_hash": incident_id_hash,
        "target_authorization_hash": target_authorization_hash,
        "issued_at_epoch_seconds": issued_at_epoch_seconds,
        "expires_at_epoch_seconds": expires_at_epoch_seconds,
        "operator_identity_hash": operator_identity_hash,
        "reason_code": reason_code,
        "complete": complete,
    }
    _validate_command_fields(unsigned)
    return RecoveryCancellationCommand(**unsigned, command_hash=_hash(unsigned))


def evaluate_recovery_cancellation_command(
    authority_state: Any,
    command: Any,
    *,
    evaluated_at_epoch_seconds: int,
    max_command_ttl_seconds: int = 300,
) -> RecoveryCancellationResult:
    for value in (evaluated_at_epoch_seconds, max_command_ttl_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RecoveryCancellationCommandError("COMMAND_BOUND_INVALID")
    state = _validated_authority_state(authority_state)
    item = _validated_command(command)
    incident_bound = item.incident_id_hash == state.incident_id_hash
    authorization_bound = item.target_authorization_hash == state.authorization_hash
    ttl = item.expires_at_epoch_seconds - item.issued_at_epoch_seconds

    if not item.complete:
        status: CancellationStatus = "INCOMPLETE"
        reasons = ["RECOVERY_CANCELLATION_COMMAND_INCOMPLETE"]
    elif not incident_bound or not authorization_bound:
        status = "TAMPERED"
        reasons = []
        if not incident_bound:
            reasons.append("CANCELLATION_INCIDENT_BINDING_MISMATCH")
        if not authorization_bound:
            reasons.append("CANCELLATION_AUTHORIZATION_BINDING_MISMATCH")
    elif item.issued_at_epoch_seconds > evaluated_at_epoch_seconds:
        status = "DENIED"
        reasons = ["RECOVERY_CANCELLATION_COMMAND_FROM_FUTURE"]
    elif ttl < 0 or ttl > max_command_ttl_seconds:
        status = "DENIED"
        reasons = ["RECOVERY_CANCELLATION_COMMAND_TTL_INVALID"]
    elif evaluated_at_epoch_seconds > item.expires_at_epoch_seconds:
        status = "STALE"
        reasons = ["RECOVERY_CANCELLATION_COMMAND_EXPIRED"]
    elif not state.active:
        status = "ALREADY_CANCELLED"
        reasons = []
    else:
        status = "CANCELLED"
        reasons = []

    applied = status == "CANCELLED"
    validated = status in {"CANCELLED", "ALREADY_CANCELLED"}
    unsigned = {
        "schema_version": COMMAND_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "command_id_hash": _hash(item.command_id),
        "command_hash": item.command_hash,
        "incident_id_hash": state.incident_id_hash,
        "target_authorization_hash": state.authorization_hash,
        "authority_state_hash": state.state_hash,
        "operator_identity_hash": item.operator_identity_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "max_command_ttl_seconds": max_command_ttl_seconds,
        "authority_active_before": state.active,
        "authority_active_after": False,
        "read_only": True,
        "cancellation_validated": validated,
        "cancellation_applied": applied,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return RecoveryCancellationResult(
        status=status,
        reasons=tuple(reasons),
        command_id_hash=unsigned["command_id_hash"],
        command_hash=item.command_hash,
        incident_id_hash=state.incident_id_hash,
        target_authorization_hash=state.authorization_hash,
        authority_state_hash=state.state_hash,
        operator_identity_hash=item.operator_identity_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        max_command_ttl_seconds=max_command_ttl_seconds,
        authority_active_before=state.active,
        authority_active_after=False,
        result_hash=_hash(unsigned),
        cancellation_validated=validated,
        cancellation_applied=applied,
    )


def validate_recovery_cancellation_result(result: Any) -> None:
    if not isinstance(result, RecoveryCancellationResult):
        raise RecoveryCancellationCommandError("RESULT_TYPE_INVALID")
    if result.read_only is not True or result.authority_active_after is not False or any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise RecoveryCancellationCommandError("RESULT_SAFETY_BOUNDARY_INVALID")
    if result.status == "CANCELLED" and (
        not result.cancellation_validated or not result.cancellation_applied
    ):
        raise RecoveryCancellationCommandError("RESULT_STATUS_INVALID")
    unsigned = asdict(result)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = COMMAND_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.result_hash != _hash(unsigned):
        raise RecoveryCancellationCommandError("RESULT_HASH_MISMATCH")


def _validated_authority_state(value: Any) -> RecoveryAuthorityState:
    if not isinstance(value, RecoveryAuthorityState):
        raise RecoveryCancellationCommandError("AUTHORITY_STATE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("state_hash")
    _validate_authority_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise RecoveryCancellationCommandError("AUTHORITY_STATE_HASH_MISMATCH")
    return value


def _validated_command(value: Any) -> RecoveryCancellationCommand:
    if not isinstance(value, RecoveryCancellationCommand):
        raise RecoveryCancellationCommandError("COMMAND_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("command_hash")
    _validate_command_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise RecoveryCancellationCommandError("COMMAND_HASH_MISMATCH")
    return value


def _validate_authority_fields(payload: dict[str, Any]) -> None:
    for key in ("incident_id_hash", "authorization_hash"):
        if not _is_sha256(payload[key]):
            raise RecoveryCancellationCommandError("AUTHORITY_STATE_FIELD_INVALID")
    if not isinstance(payload["active"], bool):
        raise RecoveryCancellationCommandError("AUTHORITY_STATE_FIELD_INVALID")
    for key in ("issued_at_epoch_seconds", "expires_at_epoch_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecoveryCancellationCommandError("AUTHORITY_STATE_FIELD_INVALID")


def _validate_command_fields(payload: dict[str, Any]) -> None:
    for key in ("incident_id_hash", "target_authorization_hash", "operator_identity_hash"):
        if not _is_sha256(payload[key]):
            raise RecoveryCancellationCommandError("COMMAND_FIELD_INVALID")
    for key in ("command_id", "reason_code"):
        if not isinstance(payload[key], str) or not payload[key].strip() or len(payload[key]) > 128:
            raise RecoveryCancellationCommandError("COMMAND_FIELD_INVALID")
    for key in ("issued_at_epoch_seconds", "expires_at_epoch_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecoveryCancellationCommandError("COMMAND_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise RecoveryCancellationCommandError("COMMAND_FIELD_INVALID")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
