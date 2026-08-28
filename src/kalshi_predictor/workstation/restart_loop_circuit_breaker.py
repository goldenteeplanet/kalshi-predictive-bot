from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

BREAKER_SCHEMA_VERSION = "phase4jg-restart-loop-circuit-breaker-v1"
PostBootStatus = Literal["NONE", "PENDING", "PASSED", "FAILED"]
BreakerStatus = Literal["CLOSED", "OPEN", "DENIED", "TAMPERED"]


class RestartLoopCircuitBreakerError(ValueError):
    """Stable fail-closed restart-loop circuit breaker error."""


@dataclass(frozen=True)
class RestartLoopState:
    incident_id_hash: str
    history_file_hash: str
    latest_intent_hash: str
    restart_attempts_for_incident: int
    post_boot_status: str
    automatic_recovery_disabled: bool
    operator_intervention_required: bool
    state_complete: bool
    integrity_verified: bool
    state_hash: str


@dataclass(frozen=True)
class RestartLoopDecision:
    status: BreakerStatus
    reasons: tuple[str, ...]
    incident_id_hash: str
    state_hash: str
    restart_attempts_for_incident: int
    post_boot_status: str
    decision_hash: str
    read_only: bool = True
    circuit_closed: bool = False
    automatic_recovery_allowed: bool = False
    operator_intervention_required: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_loop_state(**fields: Any) -> RestartLoopState:
    _validate_fields(fields)
    return RestartLoopState(**fields, state_hash=_hash(fields))


def evaluate_restart_loop_circuit_breaker(state: Any) -> RestartLoopDecision:
    item = _validated_state(state)
    contradiction = (
        (item.post_boot_status == "NONE" and item.restart_attempts_for_incident != 0)
        or (item.post_boot_status != "NONE" and item.restart_attempts_for_incident == 0)
        or (item.post_boot_status == "FAILED" and not item.automatic_recovery_disabled)
        or (item.post_boot_status == "FAILED" and not item.operator_intervention_required)
    )
    if not item.state_complete or not item.integrity_verified:
        status: BreakerStatus = "DENIED"
        reasons = ["RESTART_LOOP_STATE_UNTRUSTED"]
    elif contradiction:
        status = "TAMPERED"
        reasons = ["RESTART_LOOP_STATE_CONTRADICTION"]
    else:
        reasons = []
        if item.restart_attempts_for_incident >= 1:
            reasons.append("RESTART_LOOP_INCIDENT_ATTEMPT_CONSUMED")
        if item.post_boot_status == "PENDING":
            reasons.append("RESTART_LOOP_POST_BOOT_PENDING")
        if item.post_boot_status == "FAILED":
            reasons.append("RESTART_LOOP_POST_BOOT_FAILED")
        if item.automatic_recovery_disabled:
            reasons.append("RESTART_LOOP_AUTOMATIC_RECOVERY_DISABLED")
        if item.operator_intervention_required:
            reasons.append("RESTART_LOOP_OPERATOR_REQUIRED")
        status = "OPEN" if reasons else "CLOSED"
    closed = status == "CLOSED"
    unsigned = {
        "schema_version": BREAKER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "incident_id_hash": item.incident_id_hash,
        "state_hash": item.state_hash,
        "restart_attempts_for_incident": item.restart_attempts_for_incident,
        "post_boot_status": item.post_boot_status,
        "read_only": True,
        "circuit_closed": closed,
        "automatic_recovery_allowed": closed,
        "operator_intervention_required": not closed,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartLoopDecision(
        status=status,
        reasons=tuple(reasons),
        incident_id_hash=item.incident_id_hash,
        state_hash=item.state_hash,
        restart_attempts_for_incident=item.restart_attempts_for_incident,
        post_boot_status=item.post_boot_status,
        decision_hash=_hash(unsigned),
        circuit_closed=closed,
        automatic_recovery_allowed=closed,
        operator_intervention_required=not closed,
    )


def validate_restart_loop_decision(value: Any) -> None:
    if not isinstance(value, RestartLoopDecision):
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_DECISION_TYPE_INVALID")
    closed = value.status == "CLOSED"
    if (
        value.read_only is not True
        or value.circuit_closed != closed
        or value.automatic_recovery_allowed != closed
        or value.operator_intervention_required != (not closed)
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = BREAKER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_DECISION_HASH_MISMATCH")


def _validated_state(value: Any) -> RestartLoopState:
    if not isinstance(value, RestartLoopState):
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_STATE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("state_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_STATE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_id_hash",
        "history_file_hash",
        "latest_intent_hash",
        "restart_attempts_for_incident",
        "post_boot_status",
        "automatic_recovery_disabled",
        "operator_intervention_required",
        "state_complete",
        "integrity_verified",
    }
    if set(fields) != required:
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_STATE_FIELD_INVALID")
    for key in ("incident_id_hash", "history_file_hash", "latest_intent_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise RestartLoopCircuitBreakerError("RESTART_LOOP_STATE_FIELD_INVALID")
    if (
        isinstance(fields["restart_attempts_for_incident"], bool)
        or not isinstance(fields["restart_attempts_for_incident"], int)
        or fields["restart_attempts_for_incident"] < 0
        or fields["restart_attempts_for_incident"] > 1
        or fields["post_boot_status"] not in {"NONE", "PENDING", "PASSED", "FAILED"}
    ):
        raise RestartLoopCircuitBreakerError("RESTART_LOOP_STATE_FIELD_INVALID")
    for key in (
        "automatic_recovery_disabled",
        "operator_intervention_required",
        "state_complete",
        "integrity_verified",
    ):
        if not isinstance(fields[key], bool):
            raise RestartLoopCircuitBreakerError("RESTART_LOOP_STATE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
