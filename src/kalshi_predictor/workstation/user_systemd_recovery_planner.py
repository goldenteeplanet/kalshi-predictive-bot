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

PLANNER_SCHEMA_VERSION = "phase4is-user-systemd-recovery-planner-v1"
PlanStatus = Literal["PLANNED", "NO_ACTION", "DENIED", "INCOMPLETE", "TAMPERED"]


class UserSystemdRecoveryPlannerError(ValueError):
    """Stable fail-closed user-systemd recovery planner error."""


@dataclass(frozen=True)
class UserSystemdRecoveryContext:
    capability_decision_hash: str
    systemd_evidence_hash: str
    target_id_hash: str
    scope: str
    manager_state: str
    evidence_complete: bool
    context_hash: str


@dataclass(frozen=True)
class UserSystemdRecoveryPlan:
    status: PlanStatus
    reasons: tuple[str, ...]
    capability_decision_hash: str
    systemd_evidence_hash: str
    target_id_hash: str
    scope: str
    manager_state: str
    timeout_seconds: int
    maximum_attempts: int
    steps: tuple[str, ...]
    plan_hash: str
    read_only: bool = True
    dry_run: bool = True
    planning_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    system_scope_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_user_systemd_recovery_context(**fields: Any) -> UserSystemdRecoveryContext:
    _validate_context_fields(fields)
    return UserSystemdRecoveryContext(**fields, context_hash=_hash(fields))


def plan_user_systemd_recovery(capability: Any, context: Any) -> UserSystemdRecoveryPlan:
    if not isinstance(capability, RecoveryActionCapabilityDecision):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CAPABILITY_TYPE_INVALID")
    try:
        validate_recovery_action_capability_decision(capability)
    except ValueError as exc:
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CAPABILITY_INVALID") from exc
    item = _validated_context(context)
    bound = (
        item.capability_decision_hash == capability.decision_hash
        and item.target_id_hash == capability.target_id_hash
    )
    if not bound:
        status: PlanStatus = "TAMPERED"
        reasons = ["USER_SYSTEMD_CONTEXT_BINDING_MISMATCH"]
        steps: list[str] = []
    elif item.scope != "USER":
        status = "DENIED"
        reasons = ["SYSTEM_SCOPE_RECOVERY_PROHIBITED"]
        steps = []
    elif not item.evidence_complete:
        status = "INCOMPLETE"
        reasons = ["USER_SYSTEMD_EVIDENCE_INCOMPLETE"]
        steps = []
    elif (
        capability.status != "CAPABLE"
        or capability.action != "USER_SYSTEMD_RECOVER"
        or not capability.planning_authorized
    ):
        status = "DENIED"
        reasons = ["USER_SYSTEMD_CAPABILITY_DENIED"]
        steps = []
    elif item.manager_state == "REACHABLE":
        status = "NO_ACTION"
        reasons = ["USER_SYSTEMD_ALREADY_REACHABLE"]
        steps = ["VERIFY_USER_MANAGER_REACHABILITY", "VERIFY_USER_UNITS"]
    elif item.manager_state in {"UNREACHABLE", "DEGRADED"}:
        status = "PLANNED"
        reasons = []
        steps = [
            "VERIFY_USER_SCOPE",
            "RESTORE_USER_MANAGER",
            "VERIFY_USER_MANAGER_REACHABILITY",
            "VERIFY_USER_UNITS",
        ]
    else:
        status = "DENIED"
        reasons = ["USER_SYSTEMD_STATE_UNKNOWN"]
        steps = []
    complete = status in {"PLANNED", "NO_ACTION"}
    unsigned = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "capability_decision_hash": capability.decision_hash,
        "systemd_evidence_hash": item.systemd_evidence_hash,
        "target_id_hash": item.target_id_hash,
        "scope": item.scope,
        "manager_state": item.manager_state,
        "timeout_seconds": capability.maximum_timeout_seconds,
        "maximum_attempts": capability.maximum_attempts,
        "steps": steps,
        "read_only": True,
        "dry_run": True,
        "planning_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "system_scope_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return UserSystemdRecoveryPlan(
        status=status,
        reasons=tuple(reasons),
        capability_decision_hash=capability.decision_hash,
        systemd_evidence_hash=item.systemd_evidence_hash,
        target_id_hash=item.target_id_hash,
        scope=item.scope,
        manager_state=item.manager_state,
        timeout_seconds=capability.maximum_timeout_seconds,
        maximum_attempts=capability.maximum_attempts,
        steps=tuple(steps),
        plan_hash=_hash(unsigned),
        planning_complete=complete,
    )


def validate_user_systemd_recovery_plan(value: Any) -> None:
    if not isinstance(value, UserSystemdRecoveryPlan):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_PLAN_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.dry_run is not True
        or value.maximum_attempts != 1
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.system_scope_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_PLAN_SAFETY_BOUNDARY_INVALID")
    if value.planning_complete != (value.status in {"PLANNED", "NO_ACTION"}):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_PLAN_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("plan_hash")
    unsigned["schema_version"] = PLANNER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["steps"] = list(unsigned["steps"])
    if value.plan_hash != _hash(unsigned):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_PLAN_HASH_MISMATCH")


def _validated_context(value: Any) -> UserSystemdRecoveryContext:
    if not isinstance(value, UserSystemdRecoveryContext):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CONTEXT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("context_hash")
    _validate_context_fields(unsigned)
    if supplied != _hash(unsigned):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CONTEXT_HASH_MISMATCH")
    return value


def _validate_context_fields(fields: dict[str, Any]) -> None:
    required = {
        "capability_decision_hash",
        "systemd_evidence_hash",
        "target_id_hash",
        "scope",
        "manager_state",
        "evidence_complete",
    }
    if set(fields) != required:
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CONTEXT_FIELD_INVALID")
    for key in ("capability_decision_hash", "systemd_evidence_hash", "target_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CONTEXT_FIELD_INVALID")
    if (
        fields["scope"] not in {"USER", "SYSTEM"}
        or fields["manager_state"] not in {"REACHABLE", "UNREACHABLE", "DEGRADED", "UNKNOWN"}
        or not isinstance(fields["evidence_complete"], bool)
    ):
        raise UserSystemdRecoveryPlannerError("USER_SYSTEMD_CONTEXT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
