from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

MODEL_SCHEMA_VERSION = "phase4ip-recovery-action-capability-model-v1"
Action = Literal[
    "WSL_WAKE",
    "KEEPALIVE_RESTORE",
    "USER_SYSTEMD_RECOVER",
    "SCHEDULER_RESTORE",
    "DATABASE_READABILITY_CHECK",
]
CapabilityStatus = Literal["CAPABLE", "DENIED", "INCOMPLETE", "TAMPERED"]
ACTION_TIMEOUT_LIMITS = {
    "WSL_WAKE": 60,
    "KEEPALIVE_RESTORE": 60,
    "USER_SYSTEMD_RECOVER": 90,
    "SCHEDULER_RESTORE": 120,
    "DATABASE_READABILITY_CHECK": 30,
}


class RecoveryActionCapabilityModelError(ValueError):
    """Stable fail-closed recovery action capability model error."""


@dataclass(frozen=True)
class RecoveryActionRequest:
    request_id_hash: str
    incident_id_hash: str
    action: str
    target_id_hash: str
    requested_timeout_seconds: int
    requested_attempts: int
    dry_run: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class RecoveryActionCapabilityDecision:
    status: CapabilityStatus
    reasons: tuple[str, ...]
    request_hash: str
    incident_id_hash: str
    action: str
    target_id_hash: str
    maximum_timeout_seconds: int
    maximum_attempts: int
    capability_matrix_hash: str
    decision_hash: str
    read_only: bool = True
    planning_authorized: bool = False
    dry_run_required: bool = True
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    wsl_shutdown_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_action_request(**fields: Any) -> RecoveryActionRequest:
    _validate_fields(fields)
    return RecoveryActionRequest(**fields, request_hash=_hash(fields))


def evaluate_recovery_action_capability(request: Any) -> RecoveryActionCapabilityDecision:
    item = _validated_request(request)
    timeout_limit = ACTION_TIMEOUT_LIMITS.get(item.action, 0)
    if not item.complete:
        status: CapabilityStatus = "INCOMPLETE"
        reasons = ["RECOVERY_ACTION_REQUEST_INCOMPLETE"]
    elif item.action not in ACTION_TIMEOUT_LIMITS:
        status = "DENIED"
        reasons = [f"RECOVERY_ACTION_NOT_ALLOWLISTED:{item.action}"]
    elif not item.dry_run:
        status = "DENIED"
        reasons = ["RECOVERY_ACTION_DRY_RUN_REQUIRED"]
    elif item.requested_attempts != 1:
        status = "DENIED"
        reasons = ["RECOVERY_ACTION_ATTEMPT_BOUND_EXCEEDED"]
    elif item.requested_timeout_seconds > timeout_limit:
        status = "DENIED"
        reasons = ["RECOVERY_ACTION_TIMEOUT_BOUND_EXCEEDED"]
    else:
        status = "CAPABLE"
        reasons = []
    capable = status == "CAPABLE"
    matrix_hash = _hash(ACTION_TIMEOUT_LIMITS)
    unsigned = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "request_hash": item.request_hash,
        "incident_id_hash": item.incident_id_hash,
        "action": item.action,
        "target_id_hash": item.target_id_hash,
        "maximum_timeout_seconds": timeout_limit,
        "maximum_attempts": 1,
        "capability_matrix_hash": matrix_hash,
        "read_only": True,
        "planning_authorized": capable,
        "dry_run_required": True,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "wsl_shutdown_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return RecoveryActionCapabilityDecision(
        status=status,
        reasons=tuple(reasons),
        request_hash=item.request_hash,
        incident_id_hash=item.incident_id_hash,
        action=item.action,
        target_id_hash=item.target_id_hash,
        maximum_timeout_seconds=timeout_limit,
        maximum_attempts=1,
        capability_matrix_hash=matrix_hash,
        decision_hash=_hash(unsigned),
        planning_authorized=capable,
    )


def validate_recovery_action_capability_decision(value: Any) -> None:
    if not isinstance(value, RecoveryActionCapabilityDecision):
        raise RecoveryActionCapabilityModelError("CAPABILITY_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.dry_run_required is not True
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.wsl_shutdown_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise RecoveryActionCapabilityModelError("CAPABILITY_SAFETY_BOUNDARY_INVALID")
    if value.planning_authorized != (value.status == "CAPABLE"):
        raise RecoveryActionCapabilityModelError("CAPABILITY_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = MODEL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise RecoveryActionCapabilityModelError("CAPABILITY_DECISION_HASH_MISMATCH")


def _validated_request(value: Any) -> RecoveryActionRequest:
    if not isinstance(value, RecoveryActionRequest):
        raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "request_id_hash",
        "incident_id_hash",
        "action",
        "target_id_hash",
        "requested_timeout_seconds",
        "requested_attempts",
        "dry_run",
        "complete",
    }
    if set(fields) != required:
        raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_FIELD_INVALID")
    for key in ("request_id_hash", "incident_id_hash", "target_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_FIELD_INVALID")
    if (
        not isinstance(fields["action"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["action"]) is None
    ):
        raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_FIELD_INVALID")
    for key in ("requested_timeout_seconds", "requested_attempts"):
        item = fields[key]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_FIELD_INVALID")
    if not isinstance(fields["dry_run"], bool) or not isinstance(fields["complete"], bool):
        raise RecoveryActionCapabilityModelError("CAPABILITY_REQUEST_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
