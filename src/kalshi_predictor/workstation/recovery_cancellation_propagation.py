from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .recovery_cancellation_command import (
    RecoveryCancellationResult,
    validate_recovery_cancellation_result,
)

PROPAGATION_SCHEMA_VERSION = "phase4iw-recovery-cancellation-propagation-v1"
PropagationStatus = Literal["PROPAGATED", "NO_ACTION", "DENIED", "INCOMPLETE", "TAMPERED"]


class RecoveryCancellationPropagationError(ValueError):
    """Stable fail-closed recovery cancellation propagation error."""


@dataclass(frozen=True)
class RecoveryChildState:
    child_plan_hash: str
    parent_plan_hash: str
    state: str
    cancelable: bool
    complete: bool
    state_hash: str


@dataclass(frozen=True)
class RecoveryCancellationPropagationDecision:
    status: PropagationStatus
    reasons: tuple[str, ...]
    cancellation_result_hash: str
    parent_plan_hash: str
    child_count: int
    transitions: tuple[tuple[str, str, str], ...]
    child_set_hash: str
    decision_hash: str
    read_only: bool = True
    cancellation_propagation_proven: bool = False
    cancellation_executed: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_child_state(**fields: Any) -> RecoveryChildState:
    _validate_child_fields(fields)
    return RecoveryChildState(**fields, state_hash=_hash(fields))


def propagate_recovery_cancellation(
    cancellation: Any, parent_plan_hash: str, children: Sequence[Any], *, max_children: int = 16
) -> RecoveryCancellationPropagationDecision:
    if not isinstance(cancellation, RecoveryCancellationResult):
        raise RecoveryCancellationPropagationError("CANCELLATION_RESULT_TYPE_INVALID")
    try:
        validate_recovery_cancellation_result(cancellation)
    except ValueError as exc:
        raise RecoveryCancellationPropagationError("CANCELLATION_RESULT_INVALID") from exc
    if not _is_hash(parent_plan_hash):
        raise RecoveryCancellationPropagationError("CANCELLATION_PARENT_HASH_INVALID")
    if isinstance(max_children, bool) or not isinstance(max_children, int) or max_children <= 0:
        raise RecoveryCancellationPropagationError("CANCELLATION_BOUND_INVALID")
    if isinstance(children, (str, bytes)) or len(children) > max_children:
        raise RecoveryCancellationPropagationError("CANCELLATION_CHILD_BOUND_EXCEEDED")
    records = [_validated_child(item) for item in children]
    records.sort(key=lambda item: item.child_plan_hash)
    ids = [item.child_plan_hash for item in records]
    mismatched = any(item.parent_plan_hash != parent_plan_hash for item in records)
    active_uncancelable = any(
        item.state in {"PENDING", "RUNNING"} and not item.cancelable for item in records
    )
    if len(set(ids)) != len(ids) or mismatched:
        status: PropagationStatus = "TAMPERED"
        reasons = ["CANCELLATION_CHILD_DUPLICATE_OR_PARENT_MISMATCH"]
        transitions: list[tuple[str, str, str]] = []
    elif any(not item.complete for item in records):
        status = "INCOMPLETE"
        reasons = ["CANCELLATION_CHILD_STATE_INCOMPLETE"]
        transitions = []
    elif (
        cancellation.status not in {"CANCELLED", "ALREADY_CANCELLED"}
        or not cancellation.cancellation_validated
    ):
        status = "DENIED"
        reasons = [f"CANCELLATION_NOT_VALIDATED:{cancellation.status}"]
        transitions = []
    elif active_uncancelable:
        status = "DENIED"
        reasons = ["ACTIVE_CHILD_NOT_CANCELABLE"]
        transitions = []
    else:
        transitions = [
            (
                item.child_plan_hash,
                item.state,
                "CANCELLED" if item.state in {"PENDING", "RUNNING"} else item.state,
            )
            for item in records
        ]
        changed = any(before != after for _, before, after in transitions)
        status = "PROPAGATED" if changed else "NO_ACTION"
        reasons = [] if changed else ["NO_ACTIVE_CHILDREN_TO_CANCEL"]
    proven = status in {"PROPAGATED", "NO_ACTION"}
    child_set_hash = _hash([asdict(item) for item in records])
    unsigned = {
        "schema_version": PROPAGATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "cancellation_result_hash": cancellation.result_hash,
        "parent_plan_hash": parent_plan_hash,
        "child_count": len(records),
        "transitions": [list(item) for item in transitions],
        "child_set_hash": child_set_hash,
        "read_only": True,
        "cancellation_propagation_proven": proven,
        "cancellation_executed": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return RecoveryCancellationPropagationDecision(
        status=status,
        reasons=tuple(reasons),
        cancellation_result_hash=cancellation.result_hash,
        parent_plan_hash=parent_plan_hash,
        child_count=len(records),
        transitions=tuple(transitions),
        child_set_hash=child_set_hash,
        decision_hash=_hash(unsigned),
        cancellation_propagation_proven=proven,
    )


def validate_recovery_cancellation_propagation_decision(value: Any) -> None:
    if not isinstance(value, RecoveryCancellationPropagationDecision):
        raise RecoveryCancellationPropagationError("PROPAGATION_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.cancellation_executed is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise RecoveryCancellationPropagationError("PROPAGATION_SAFETY_BOUNDARY_INVALID")
    if value.cancellation_propagation_proven != (value.status in {"PROPAGATED", "NO_ACTION"}):
        raise RecoveryCancellationPropagationError("PROPAGATION_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = PROPAGATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["transitions"] = [list(item) for item in unsigned["transitions"]]
    if value.decision_hash != _hash(unsigned):
        raise RecoveryCancellationPropagationError("PROPAGATION_DECISION_HASH_MISMATCH")


def _validated_child(value: Any) -> RecoveryChildState:
    if not isinstance(value, RecoveryChildState):
        raise RecoveryCancellationPropagationError("CANCELLATION_CHILD_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("state_hash")
    _validate_child_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RecoveryCancellationPropagationError("CANCELLATION_CHILD_HASH_MISMATCH")
    return value


def _validate_child_fields(fields: dict[str, Any]) -> None:
    required = {"child_plan_hash", "parent_plan_hash", "state", "cancelable", "complete"}
    if (
        set(fields) != required
        or not _is_hash(fields["child_plan_hash"])
        or not _is_hash(fields["parent_plan_hash"])
    ):
        raise RecoveryCancellationPropagationError("CANCELLATION_CHILD_FIELD_INVALID")
    if fields["state"] not in {"PENDING", "RUNNING", "COMPLETE", "CANCELLED", "FAILED"}:
        raise RecoveryCancellationPropagationError("CANCELLATION_CHILD_FIELD_INVALID")
    if not isinstance(fields["cancelable"], bool) or not isinstance(fields["complete"], bool):
        raise RecoveryCancellationPropagationError("CANCELLATION_CHILD_FIELD_INVALID")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
