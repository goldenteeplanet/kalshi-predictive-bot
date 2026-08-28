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

PLANNER_SCHEMA_VERSION = "phase4iu-database-readability-recovery-planner-v1"
PlanStatus = Literal["PLANNED", "NO_ACTION", "DENIED", "INCOMPLETE", "TAMPERED"]


class DatabaseReadabilityRecoveryPlannerError(ValueError):
    """Stable fail-closed database readability recovery planner error."""


@dataclass(frozen=True)
class DatabaseReadabilityRecoveryContext:
    capability_decision_hash: str
    classifier_decision_hash: str
    target_id_hash: str
    readability_state: str
    evidence_complete: bool
    context_hash: str


@dataclass(frozen=True)
class DatabaseReadabilityRecoveryPlan:
    status: PlanStatus
    reasons: tuple[str, ...]
    capability_decision_hash: str
    classifier_decision_hash: str
    target_id_hash: str
    readability_state: str
    timeout_seconds: int
    maximum_attempts: int
    steps: tuple[str, ...]
    plan_hash: str
    read_only: bool = True
    dry_run: bool = True
    planning_complete: bool = False
    database_write_authorized: bool = False
    database_restore_authorized: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_database_readability_recovery_context(**fields: Any) -> DatabaseReadabilityRecoveryContext:
    _validate_context_fields(fields)
    return DatabaseReadabilityRecoveryContext(**fields, context_hash=_hash(fields))


def plan_database_readability_recovery(
    capability: Any, context: Any
) -> DatabaseReadabilityRecoveryPlan:
    if not isinstance(capability, RecoveryActionCapabilityDecision):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CAPABILITY_TYPE_INVALID")
    try:
        validate_recovery_action_capability_decision(capability)
    except ValueError as exc:
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CAPABILITY_INVALID") from exc
    item = _validated_context(context)
    bound = (
        item.capability_decision_hash == capability.decision_hash
        and item.target_id_hash == capability.target_id_hash
    )
    if not bound:
        status: PlanStatus = "TAMPERED"
        reasons = ["DATABASE_CONTEXT_BINDING_MISMATCH"]
        steps: list[str] = []
    elif not item.evidence_complete:
        status = "INCOMPLETE"
        reasons = ["DATABASE_READABILITY_EVIDENCE_INCOMPLETE"]
        steps = []
    elif (
        capability.status != "CAPABLE"
        or capability.action != "DATABASE_READABILITY_CHECK"
        or not capability.planning_authorized
    ):
        status = "DENIED"
        reasons = ["DATABASE_READABILITY_CAPABILITY_DENIED"]
        steps = []
    elif item.readability_state == "READABLE":
        status = "NO_ACTION"
        reasons = ["DATABASE_ALREADY_READABLE"]
        steps = ["VERIFY_DATABASE_READABILITY", "VERIFY_PROTECTED_INVARIANTS"]
    elif item.readability_state == "UNREADABLE":
        status = "PLANNED"
        reasons = []
        steps = [
            "VERIFY_DATABASE_PATH_METADATA",
            "RUN_READ_ONLY_INTEGRITY_CHECK",
            "CAPTURE_DIAGNOSTIC_RESULT",
            "ESCALATE_OPERATOR",
        ]
    elif item.readability_state == "INCOMPLETE":
        status = "INCOMPLETE"
        reasons = ["DATABASE_CLASSIFICATION_INCOMPLETE"]
        steps = []
    else:
        status = "DENIED"
        reasons = [f"DATABASE_READABILITY_STATE_NOT_ACTIONABLE:{item.readability_state}"]
        steps = []
    complete = status in {"PLANNED", "NO_ACTION"}
    unsigned = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "capability_decision_hash": capability.decision_hash,
        "classifier_decision_hash": item.classifier_decision_hash,
        "target_id_hash": item.target_id_hash,
        "readability_state": item.readability_state,
        "timeout_seconds": capability.maximum_timeout_seconds,
        "maximum_attempts": capability.maximum_attempts,
        "steps": steps,
        "read_only": True,
        "dry_run": True,
        "planning_complete": complete,
        "database_write_authorized": False,
        "database_restore_authorized": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DatabaseReadabilityRecoveryPlan(
        status=status,
        reasons=tuple(reasons),
        capability_decision_hash=capability.decision_hash,
        classifier_decision_hash=item.classifier_decision_hash,
        target_id_hash=item.target_id_hash,
        readability_state=item.readability_state,
        timeout_seconds=capability.maximum_timeout_seconds,
        maximum_attempts=capability.maximum_attempts,
        steps=tuple(steps),
        plan_hash=_hash(unsigned),
        planning_complete=complete,
    )


def validate_database_readability_recovery_plan(value: Any) -> None:
    if not isinstance(value, DatabaseReadabilityRecoveryPlan):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_PLAN_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.dry_run is not True
        or value.maximum_attempts != 1
        or any(
            (
                value.database_write_authorized,
                value.database_restore_authorized,
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_PLAN_SAFETY_BOUNDARY_INVALID")
    if value.planning_complete != (value.status in {"PLANNED", "NO_ACTION"}):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_PLAN_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("plan_hash")
    unsigned["schema_version"] = PLANNER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["steps"] = list(unsigned["steps"])
    if value.plan_hash != _hash(unsigned):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_PLAN_HASH_MISMATCH")


def _validated_context(value: Any) -> DatabaseReadabilityRecoveryContext:
    if not isinstance(value, DatabaseReadabilityRecoveryContext):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CONTEXT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("context_hash")
    _validate_context_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CONTEXT_HASH_MISMATCH")
    return value


def _validate_context_fields(fields: dict[str, Any]) -> None:
    required = {
        "capability_decision_hash",
        "classifier_decision_hash",
        "target_id_hash",
        "readability_state",
        "evidence_complete",
    }
    if set(fields) != required:
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CONTEXT_FIELD_INVALID")
    for key in ("capability_decision_hash", "classifier_decision_hash", "target_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CONTEXT_FIELD_INVALID")
    if fields["readability_state"] not in {
        "READABLE",
        "UNREADABLE",
        "UNKNOWN",
        "INCOMPLETE",
        "TAMPERED",
    } or not isinstance(fields["evidence_complete"], bool):
        raise DatabaseReadabilityRecoveryPlannerError("DATABASE_CONTEXT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
