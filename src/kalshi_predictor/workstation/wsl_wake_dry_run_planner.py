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
from .wsl_status_evidence_capture import WslStatusCapture, validate_wsl_status_capture

PLANNER_SCHEMA_VERSION = "phase4iq-wsl-wake-dry-run-planner-v1"
PlanStatus = Literal["PLANNED", "NO_ACTION", "DENIED", "INCOMPLETE", "TAMPERED"]


class WslWakeDryRunPlannerError(ValueError):
    """Stable fail-closed WSL wake dry-run planner error."""


@dataclass(frozen=True)
class WslWakeContext:
    capability_decision_hash: str
    wsl_status_capture_hash: str
    target_distribution_id_hash: str
    current_state: str
    complete: bool
    context_hash: str


@dataclass(frozen=True)
class WslWakeDryRunPlan:
    status: PlanStatus
    reasons: tuple[str, ...]
    capability_decision_hash: str
    wsl_status_capture_hash: str
    target_distribution_id_hash: str
    current_state: str
    timeout_seconds: int
    steps: tuple[str, ...]
    plan_hash: str
    read_only: bool = True
    dry_run: bool = True
    planning_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    wsl_wake_authorized: bool = False
    wsl_shutdown_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_wsl_wake_context(**fields: Any) -> WslWakeContext:
    _validate_context_fields(fields)
    return WslWakeContext(**fields, context_hash=_hash(fields))


def plan_wsl_wake_dry_run(capability: Any, wsl_status: Any, context: Any) -> WslWakeDryRunPlan:
    if not isinstance(capability, RecoveryActionCapabilityDecision):
        raise WslWakeDryRunPlannerError("WSL_WAKE_CAPABILITY_TYPE_INVALID")
    if not isinstance(wsl_status, WslStatusCapture):
        raise WslWakeDryRunPlannerError("WSL_WAKE_STATUS_TYPE_INVALID")
    try:
        validate_recovery_action_capability_decision(capability)
        validate_wsl_status_capture(wsl_status)
    except ValueError as exc:
        raise WslWakeDryRunPlannerError("WSL_WAKE_UPSTREAM_INVALID") from exc
    item = _validated_context(context)
    bound = (
        item.capability_decision_hash == capability.decision_hash
        and item.wsl_status_capture_hash == wsl_status.capture_hash
        and item.target_distribution_id_hash == capability.target_id_hash
    )
    if not bound:
        status: PlanStatus = "TAMPERED"
        reasons = ["WSL_WAKE_CONTEXT_BINDING_MISMATCH"]
        steps: list[str] = []
    elif not item.complete or wsl_status.status != "CAPTURED":
        status = "INCOMPLETE"
        reasons = ["WSL_WAKE_EVIDENCE_INCOMPLETE"]
        steps = []
    elif (
        capability.status != "CAPABLE"
        or capability.action != "WSL_WAKE"
        or not capability.planning_authorized
    ):
        status = "DENIED"
        reasons = ["WSL_WAKE_CAPABILITY_DENIED"]
        steps = []
    elif item.current_state == "RUNNING":
        status = "NO_ACTION"
        reasons = ["WSL_TARGET_ALREADY_RUNNING"]
        steps = ["VERIFY_TARGET_LIVENESS"]
    elif item.current_state == "STOPPED":
        status = "PLANNED"
        reasons = []
        steps = ["VERIFY_TARGET_STOPPED", "WAKE_TARGET_DISTRIBUTION", "VERIFY_TARGET_LIVENESS"]
    else:
        status = "DENIED"
        reasons = [f"WSL_TARGET_STATE_NOT_WAKEABLE:{item.current_state}"]
        steps = []
    complete = status in {"PLANNED", "NO_ACTION"}
    unsigned = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "capability_decision_hash": capability.decision_hash,
        "wsl_status_capture_hash": wsl_status.capture_hash,
        "target_distribution_id_hash": item.target_distribution_id_hash,
        "current_state": item.current_state,
        "timeout_seconds": capability.maximum_timeout_seconds,
        "steps": steps,
        "read_only": True,
        "dry_run": True,
        "planning_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "wsl_wake_authorized": False,
        "wsl_shutdown_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WslWakeDryRunPlan(
        status=status,
        reasons=tuple(reasons),
        capability_decision_hash=capability.decision_hash,
        wsl_status_capture_hash=wsl_status.capture_hash,
        target_distribution_id_hash=item.target_distribution_id_hash,
        current_state=item.current_state,
        timeout_seconds=capability.maximum_timeout_seconds,
        steps=tuple(steps),
        plan_hash=_hash(unsigned),
        planning_complete=complete,
    )


def validate_wsl_wake_dry_run_plan(value: Any) -> None:
    if not isinstance(value, WslWakeDryRunPlan):
        raise WslWakeDryRunPlannerError("WSL_WAKE_PLAN_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.dry_run is not True
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.wsl_wake_authorized,
                value.wsl_shutdown_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise WslWakeDryRunPlannerError("WSL_WAKE_PLAN_SAFETY_BOUNDARY_INVALID")
    if value.planning_complete != (value.status in {"PLANNED", "NO_ACTION"}):
        raise WslWakeDryRunPlannerError("WSL_WAKE_PLAN_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("plan_hash")
    unsigned["schema_version"] = PLANNER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["steps"] = list(unsigned["steps"])
    if value.plan_hash != _hash(unsigned):
        raise WslWakeDryRunPlannerError("WSL_WAKE_PLAN_HASH_MISMATCH")


def _validated_context(value: Any) -> WslWakeContext:
    if not isinstance(value, WslWakeContext):
        raise WslWakeDryRunPlannerError("WSL_WAKE_CONTEXT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("context_hash")
    _validate_context_fields(unsigned)
    if supplied != _hash(unsigned):
        raise WslWakeDryRunPlannerError("WSL_WAKE_CONTEXT_HASH_MISMATCH")
    return value


def _validate_context_fields(fields: dict[str, Any]) -> None:
    required = {
        "capability_decision_hash",
        "wsl_status_capture_hash",
        "target_distribution_id_hash",
        "current_state",
        "complete",
    }
    if set(fields) != required:
        raise WslWakeDryRunPlannerError("WSL_WAKE_CONTEXT_FIELD_INVALID")
    for key in (
        "capability_decision_hash",
        "wsl_status_capture_hash",
        "target_distribution_id_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise WslWakeDryRunPlannerError("WSL_WAKE_CONTEXT_FIELD_INVALID")
    if fields["current_state"] not in {
        "RUNNING",
        "STOPPED",
        "INSTALLING",
        "UNAVAILABLE",
        "UNKNOWN",
    } or not isinstance(fields["complete"], bool):
        raise WslWakeDryRunPlannerError("WSL_WAKE_CONTEXT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
