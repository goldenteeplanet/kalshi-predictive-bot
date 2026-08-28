from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .recovery_action_capability_model import (
    RecoveryActionCapabilityDecision,
    validate_recovery_action_capability_decision,
)

PLANNER_SCHEMA_VERSION = "phase4ir-keepalive-restoration-dry-run-planner-v1"
PlanStatus = Literal["PLANNED", "NO_ACTION", "DENIED", "INCOMPLETE", "TAMPERED"]


class KeepaliveRestorationDryRunPlannerError(ValueError):
    """Stable fail-closed keepalive restoration dry-run planner error."""


@dataclass(frozen=True)
class KeepaliveRestorationContext:
    capability_decision_hash: str
    health_evidence_hash: str
    target_id_hash: str
    current_state: str
    evidence_complete: bool
    context_hash: str


@dataclass(frozen=True)
class KeepaliveRestorationDryRunPlan:
    status: PlanStatus
    reasons: tuple[str, ...]
    capability_decision_hash: str
    health_evidence_hash: str
    target_id_hash: str
    current_state: str
    timeout_seconds: int
    maximum_attempts: int
    steps: tuple[str, ...]
    plan_hash: str
    read_only: bool = True
    dry_run: bool = True
    planning_complete: bool = False
    recovery_authorized: bool = False
    task_control_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_keepalive_restoration_context(**fields: Any) -> KeepaliveRestorationContext:
    _validate_context_fields(fields)
    return KeepaliveRestorationContext(**fields, context_hash=_hash(fields))


def plan_keepalive_restoration_dry_run(
    capability: Any, context: Any
) -> KeepaliveRestorationDryRunPlan:
    if not isinstance(capability, RecoveryActionCapabilityDecision):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CAPABILITY_TYPE_INVALID")
    try:
        validate_recovery_action_capability_decision(capability)
    except ValueError as exc:
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CAPABILITY_INVALID") from exc
    item = _validated_context(context)
    bound = (
        item.capability_decision_hash == capability.decision_hash
        and item.target_id_hash == capability.target_id_hash
    )
    if not bound:
        status: PlanStatus = "TAMPERED"
        reasons = ["KEEPALIVE_CONTEXT_BINDING_MISMATCH"]
        steps: list[str] = []
    elif not item.evidence_complete:
        status = "INCOMPLETE"
        reasons = ["KEEPALIVE_HEALTH_EVIDENCE_INCOMPLETE"]
        steps = []
    elif (
        capability.status != "CAPABLE"
        or capability.action != "KEEPALIVE_RESTORE"
        or not capability.planning_authorized
    ):
        status = "DENIED"
        reasons = ["KEEPALIVE_CAPABILITY_DENIED"]
        steps = []
    elif item.current_state == "ACTIVE":
        status = "NO_ACTION"
        reasons = ["KEEPALIVE_ALREADY_ACTIVE"]
        steps = ["VERIFY_KEEPALIVE_HEARTBEAT"]
    elif item.current_state in {"INACTIVE", "FAILED"}:
        status = "PLANNED"
        reasons = []
        steps = [
            "VERIFY_KEEPALIVE_NOT_ACTIVE",
            "RESTORE_KEEPALIVE_TASK",
            "VERIFY_KEEPALIVE_HEARTBEAT",
        ]
    else:
        status = "DENIED"
        reasons = ["KEEPALIVE_STATE_UNKNOWN"]
        steps = []
    complete = status in {"PLANNED", "NO_ACTION"}
    unsigned = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "capability_decision_hash": capability.decision_hash,
        "health_evidence_hash": item.health_evidence_hash,
        "target_id_hash": item.target_id_hash,
        "current_state": item.current_state,
        "timeout_seconds": capability.maximum_timeout_seconds,
        "maximum_attempts": capability.maximum_attempts,
        "steps": steps,
        "read_only": True,
        "dry_run": True,
        "planning_complete": complete,
        "recovery_authorized": False,
        "task_control_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return KeepaliveRestorationDryRunPlan(
        status=status,
        reasons=tuple(reasons),
        capability_decision_hash=capability.decision_hash,
        health_evidence_hash=item.health_evidence_hash,
        target_id_hash=item.target_id_hash,
        current_state=item.current_state,
        timeout_seconds=capability.maximum_timeout_seconds,
        maximum_attempts=capability.maximum_attempts,
        steps=tuple(steps),
        plan_hash=_hash(unsigned),
        planning_complete=complete,
    )


def validate_keepalive_restoration_dry_run_plan(value: Any) -> None:
    if not isinstance(value, KeepaliveRestorationDryRunPlan):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_PLAN_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.dry_run is not True
        or value.maximum_attempts != 1
        or any(
            (
                value.recovery_authorized,
                value.task_control_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_PLAN_SAFETY_BOUNDARY_INVALID")
    if value.planning_complete != (value.status in {"PLANNED", "NO_ACTION"}):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_PLAN_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("plan_hash")
    unsigned["schema_version"] = PLANNER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["steps"] = list(unsigned["steps"])
    if value.plan_hash != _hash(unsigned):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_PLAN_HASH_MISMATCH")


def _validated_context(value: Any) -> KeepaliveRestorationContext:
    if not isinstance(value, KeepaliveRestorationContext):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CONTEXT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("context_hash")
    _validate_context_fields(unsigned)
    if supplied != _hash(unsigned):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CONTEXT_HASH_MISMATCH")
    return value


def _validate_context_fields(fields: dict[str, Any]) -> None:
    required = {
        "capability_decision_hash",
        "health_evidence_hash",
        "target_id_hash",
        "current_state",
        "evidence_complete",
    }
    if set(fields) != required:
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CONTEXT_FIELD_INVALID")
    for key in ("capability_decision_hash", "health_evidence_hash", "target_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CONTEXT_FIELD_INVALID")
    if fields["current_state"] not in {"ACTIVE", "INACTIVE", "FAILED", "UNKNOWN"} or not isinstance(
        fields["evidence_complete"], bool
    ):
        raise KeepaliveRestorationDryRunPlannerError("KEEPALIVE_CONTEXT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
