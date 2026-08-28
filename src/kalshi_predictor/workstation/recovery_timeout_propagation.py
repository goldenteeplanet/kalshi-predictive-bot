from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

PROPAGATION_SCHEMA_VERSION = "phase4iv-recovery-timeout-propagation-v1"
PropagationStatus = Literal["PROPAGATED", "EXPIRED", "DENIED", "INCOMPLETE", "TAMPERED"]


class RecoveryTimeoutPropagationError(ValueError):
    """Stable fail-closed recovery timeout propagation error."""


@dataclass(frozen=True)
class RecoveryTimeoutStep:
    step_code: str
    requested_seconds: int
    complete: bool
    step_hash: str


@dataclass(frozen=True)
class RecoveryTimeoutDecision:
    status: PropagationStatus
    reasons: tuple[str, ...]
    parent_plan_hash: str
    issued_at_epoch_seconds: int
    parent_deadline_epoch_seconds: int
    evaluated_at_epoch_seconds: int
    remaining_seconds: int
    requested_total_seconds: int
    step_deadlines: tuple[tuple[str, int], ...]
    step_set_hash: str
    decision_hash: str
    read_only: bool = True
    timeout_propagation_proven: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_timeout_step(**fields: Any) -> RecoveryTimeoutStep:
    _validate_step_fields(fields)
    return RecoveryTimeoutStep(**fields, step_hash=_hash(fields))


def propagate_recovery_timeout(
    parent_plan_hash: str,
    steps: Sequence[Any],
    *,
    issued_at_epoch_seconds: int,
    parent_deadline_epoch_seconds: int,
    evaluated_at_epoch_seconds: int,
    max_steps: int = 16,
) -> RecoveryTimeoutDecision:
    if not _is_hash(parent_plan_hash):
        raise RecoveryTimeoutPropagationError("TIMEOUT_PARENT_HASH_INVALID")
    for value in (
        issued_at_epoch_seconds,
        parent_deadline_epoch_seconds,
        evaluated_at_epoch_seconds,
        max_steps,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecoveryTimeoutPropagationError("TIMEOUT_BOUND_INVALID")
    if max_steps == 0 or parent_deadline_epoch_seconds < issued_at_epoch_seconds:
        raise RecoveryTimeoutPropagationError("TIMEOUT_BOUND_INVALID")
    if isinstance(steps, (str, bytes)) or len(steps) > max_steps:
        raise RecoveryTimeoutPropagationError("TIMEOUT_STEP_BOUND_EXCEEDED")
    records = [_validated_step(item) for item in steps]
    codes = [item.step_code for item in records]
    remaining = max(0, parent_deadline_epoch_seconds - evaluated_at_epoch_seconds)
    requested = sum(item.requested_seconds for item in records)
    deadlines: list[tuple[str, int]] = []
    cursor = evaluated_at_epoch_seconds
    for item in records:
        cursor += item.requested_seconds
        deadlines.append((item.step_code, cursor))
    if len(set(codes)) != len(codes):
        status: PropagationStatus = "TAMPERED"
        reasons = ["TIMEOUT_STEP_CODE_DUPLICATE"]
        deadlines = []
    elif issued_at_epoch_seconds > evaluated_at_epoch_seconds:
        status = "TAMPERED"
        reasons = ["TIMEOUT_PARENT_FROM_FUTURE"]
        deadlines = []
    elif evaluated_at_epoch_seconds >= parent_deadline_epoch_seconds:
        status = "EXPIRED"
        reasons = ["TIMEOUT_PARENT_DEADLINE_REACHED"]
        deadlines = []
    elif not records or any(not item.complete for item in records):
        status = "INCOMPLETE"
        reasons = ["TIMEOUT_STEP_SET_EMPTY_OR_INCOMPLETE"]
        deadlines = []
    elif requested > remaining:
        status = "DENIED"
        reasons = ["TIMEOUT_STEP_BUDGET_EXCEEDS_PARENT"]
        deadlines = []
    else:
        status = "PROPAGATED"
        reasons = []
    proven = status == "PROPAGATED"
    step_set_hash = _hash([asdict(item) for item in records])
    unsigned = {
        "schema_version": PROPAGATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "parent_plan_hash": parent_plan_hash,
        "issued_at_epoch_seconds": issued_at_epoch_seconds,
        "parent_deadline_epoch_seconds": parent_deadline_epoch_seconds,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "remaining_seconds": remaining,
        "requested_total_seconds": requested,
        "step_deadlines": [list(item) for item in deadlines],
        "step_set_hash": step_set_hash,
        "read_only": True,
        "timeout_propagation_proven": proven,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return RecoveryTimeoutDecision(
        status=status,
        reasons=tuple(reasons),
        parent_plan_hash=parent_plan_hash,
        issued_at_epoch_seconds=issued_at_epoch_seconds,
        parent_deadline_epoch_seconds=parent_deadline_epoch_seconds,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        remaining_seconds=remaining,
        requested_total_seconds=requested,
        step_deadlines=tuple(deadlines),
        step_set_hash=step_set_hash,
        decision_hash=_hash(unsigned),
        timeout_propagation_proven=proven,
    )


def validate_recovery_timeout_decision(value: Any) -> None:
    if not isinstance(value, RecoveryTimeoutDecision):
        raise RecoveryTimeoutPropagationError("TIMEOUT_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise RecoveryTimeoutPropagationError("TIMEOUT_SAFETY_BOUNDARY_INVALID")
    if value.timeout_propagation_proven != (value.status == "PROPAGATED"):
        raise RecoveryTimeoutPropagationError("TIMEOUT_STATUS_INVALID")
    if any(deadline > value.parent_deadline_epoch_seconds for _, deadline in value.step_deadlines):
        raise RecoveryTimeoutPropagationError("TIMEOUT_CHILD_DEADLINE_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = PROPAGATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["step_deadlines"] = [list(item) for item in unsigned["step_deadlines"]]
    if value.decision_hash != _hash(unsigned):
        raise RecoveryTimeoutPropagationError("TIMEOUT_DECISION_HASH_MISMATCH")


def _validated_step(value: Any) -> RecoveryTimeoutStep:
    if not isinstance(value, RecoveryTimeoutStep):
        raise RecoveryTimeoutPropagationError("TIMEOUT_STEP_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("step_hash")
    _validate_step_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RecoveryTimeoutPropagationError("TIMEOUT_STEP_HASH_MISMATCH")
    return value


def _validate_step_fields(fields: dict[str, Any]) -> None:
    required = {"step_code", "requested_seconds", "complete"}
    if (
        set(fields) != required
        or not isinstance(fields["step_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["step_code"]) is None
    ):
        raise RecoveryTimeoutPropagationError("TIMEOUT_STEP_FIELD_INVALID")
    seconds = fields["requested_seconds"]
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or seconds <= 0
        or not isinstance(fields["complete"], bool)
    ):
        raise RecoveryTimeoutPropagationError("TIMEOUT_STEP_FIELD_INVALID")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
