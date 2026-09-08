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

PLANNER_SCHEMA_VERSION = "phase4it-scheduler-restoration-planner-v1"
PlanStatus = Literal["PLANNED", "NO_ACTION", "DENIED", "INCOMPLETE", "TAMPERED"]


class SchedulerRestorationPlannerError(ValueError):
    """Stable fail-closed scheduler restoration planner error."""


@dataclass(frozen=True)
class SchedulerRestorationContext:
    capability_decision_hash: str
    scheduler_evidence_hash: str
    writer_exclusivity_evidence_hash: str
    target_id_hash: str
    scheduler_state: str
    writer_exclusivity_proven: bool
    evidence_complete: bool
    context_hash: str


@dataclass(frozen=True)
class SchedulerRestorationPlan:
    status: PlanStatus
    reasons: tuple[str, ...]
    capability_decision_hash: str
    scheduler_evidence_hash: str
    writer_exclusivity_evidence_hash: str
    target_id_hash: str
    scheduler_state: str
    timeout_seconds: int
    maximum_attempts: int
    steps: tuple[str, ...]
    plan_hash: str
    read_only: bool = True
    dry_run: bool = True
    planning_complete: bool = False
    writer_exclusivity_required: bool = True
    recovery_authorized: bool = False
    scheduler_control_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_scheduler_restoration_context(**fields: Any) -> SchedulerRestorationContext:
    _validate_context_fields(fields)
    return SchedulerRestorationContext(**fields, context_hash=_hash(fields))


def plan_scheduler_restoration(capability: Any, context: Any) -> SchedulerRestorationPlan:
    if not isinstance(capability, RecoveryActionCapabilityDecision):
        raise SchedulerRestorationPlannerError("SCHEDULER_CAPABILITY_TYPE_INVALID")
    try:
        validate_recovery_action_capability_decision(capability)
    except ValueError as exc:
        raise SchedulerRestorationPlannerError("SCHEDULER_CAPABILITY_INVALID") from exc
    item = _validated_context(context)
    bound = (
        item.capability_decision_hash == capability.decision_hash
        and item.target_id_hash == capability.target_id_hash
    )
    if not bound:
        status: PlanStatus = "TAMPERED"
        reasons = ["SCHEDULER_CONTEXT_BINDING_MISMATCH"]
        steps: list[str] = []
    elif not item.evidence_complete:
        status = "INCOMPLETE"
        reasons = ["SCHEDULER_EVIDENCE_INCOMPLETE"]
        steps = []
    elif (
        capability.status != "CAPABLE"
        or capability.action != "SCHEDULER_RESTORE"
        or not capability.planning_authorized
    ):
        status = "DENIED"
        reasons = ["SCHEDULER_CAPABILITY_DENIED"]
        steps = []
    elif not item.writer_exclusivity_proven:
        status = "DENIED"
        reasons = ["SCHEDULER_WRITER_EXCLUSIVITY_UNPROVEN"]
        steps = []
    elif item.scheduler_state == "ACTIVE":
        status = "NO_ACTION"
        reasons = ["SCHEDULER_ALREADY_ACTIVE"]
        steps = [
            "VERIFY_WRITER_EXCLUSIVITY",
            "VERIFY_SCHEDULER_HEARTBEAT",
            "VERIFY_PROTECTED_INVARIANTS",
        ]
    elif item.scheduler_state in {"INACTIVE", "FAILED"}:
        status = "PLANNED"
        reasons = []
        steps = [
            "VERIFY_WRITER_EXCLUSIVITY",
            "RESTORE_USER_SCHEDULER",
            "VERIFY_SCHEDULER_HEARTBEAT",
            "VERIFY_PROTECTED_INVARIANTS",
        ]
    else:
        status = "DENIED"
        reasons = ["SCHEDULER_STATE_UNKNOWN"]
        steps = []
    complete = status in {"PLANNED", "NO_ACTION"}
    unsigned = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "capability_decision_hash": capability.decision_hash,
        "scheduler_evidence_hash": item.scheduler_evidence_hash,
        "writer_exclusivity_evidence_hash": item.writer_exclusivity_evidence_hash,
        "target_id_hash": item.target_id_hash,
        "scheduler_state": item.scheduler_state,
        "timeout_seconds": capability.maximum_timeout_seconds,
        "maximum_attempts": capability.maximum_attempts,
        "steps": steps,
        "read_only": True,
        "dry_run": True,
        "planning_complete": complete,
        "writer_exclusivity_required": True,
        "recovery_authorized": False,
        "scheduler_control_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return SchedulerRestorationPlan(
        status=status,
        reasons=tuple(reasons),
        capability_decision_hash=capability.decision_hash,
        scheduler_evidence_hash=item.scheduler_evidence_hash,
        writer_exclusivity_evidence_hash=item.writer_exclusivity_evidence_hash,
        target_id_hash=item.target_id_hash,
        scheduler_state=item.scheduler_state,
        timeout_seconds=capability.maximum_timeout_seconds,
        maximum_attempts=capability.maximum_attempts,
        steps=tuple(steps),
        plan_hash=_hash(unsigned),
        planning_complete=complete,
    )


def validate_scheduler_restoration_plan(value: Any) -> None:
    if not isinstance(value, SchedulerRestorationPlan):
        raise SchedulerRestorationPlannerError("SCHEDULER_PLAN_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.dry_run is not True
        or value.maximum_attempts != 1
        or value.writer_exclusivity_required is not True
        or any(
            (
                value.recovery_authorized,
                value.scheduler_control_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SchedulerRestorationPlannerError("SCHEDULER_PLAN_SAFETY_BOUNDARY_INVALID")
    if value.planning_complete != (value.status in {"PLANNED", "NO_ACTION"}):
        raise SchedulerRestorationPlannerError("SCHEDULER_PLAN_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("plan_hash")
    unsigned["schema_version"] = PLANNER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["steps"] = list(unsigned["steps"])
    if value.plan_hash != _hash(unsigned):
        raise SchedulerRestorationPlannerError("SCHEDULER_PLAN_HASH_MISMATCH")


def _validated_context(value: Any) -> SchedulerRestorationContext:
    if not isinstance(value, SchedulerRestorationContext):
        raise SchedulerRestorationPlannerError("SCHEDULER_CONTEXT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("context_hash")
    _validate_context_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SchedulerRestorationPlannerError("SCHEDULER_CONTEXT_HASH_MISMATCH")
    return value


def _validate_context_fields(fields: dict[str, Any]) -> None:
    required = {
        "capability_decision_hash",
        "scheduler_evidence_hash",
        "writer_exclusivity_evidence_hash",
        "target_id_hash",
        "scheduler_state",
        "writer_exclusivity_proven",
        "evidence_complete",
    }
    if set(fields) != required:
        raise SchedulerRestorationPlannerError("SCHEDULER_CONTEXT_FIELD_INVALID")
    for key in (
        "capability_decision_hash",
        "scheduler_evidence_hash",
        "writer_exclusivity_evidence_hash",
        "target_id_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SchedulerRestorationPlannerError("SCHEDULER_CONTEXT_FIELD_INVALID")
    if fields["scheduler_state"] not in {"ACTIVE", "INACTIVE", "FAILED", "UNKNOWN"}:
        raise SchedulerRestorationPlannerError("SCHEDULER_CONTEXT_FIELD_INVALID")
    if not isinstance(fields["writer_exclusivity_proven"], bool) or not isinstance(
        fields["evidence_complete"], bool
    ):
        raise SchedulerRestorationPlannerError("SCHEDULER_CONTEXT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
