from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

PROPOSAL_SCHEMA_VERSION = "phase4jt-windows-startup-task-proposal-v1"
ALLOWLISTED_TRIGGER = "AT_STARTUP"
ProposalStatus = Literal["READY", "DENIED", "INCOMPLETE", "TAMPERED"]


class WindowsStartupTaskProposalError(ValueError):
    """Stable fail-closed Windows startup-task proposal error."""


@dataclass(frozen=True)
class StartupTaskProposalRequest:
    proposal_id_hash: str
    supervisor_artifact_hash: str
    configuration_hash: str
    rollback_script_hash: str
    task_name: str
    trigger: str
    run_with_highest_privileges: bool
    network_required: bool
    activation_requested: bool
    dry_run: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class StartupTaskProposalDecision:
    status: ProposalStatus
    reasons: tuple[str, ...]
    request_hash: str
    task_name_hash: str
    trigger: str
    configuration_preview_hash: str
    rollback_preview_hash: str
    decision_hash: str
    read_only: bool = True
    proposal_ready: bool = False
    activation_disabled: bool = True
    operator_review_required: bool = True
    task_creation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_startup_task_proposal_request(**fields: Any) -> StartupTaskProposalRequest:
    _validate_fields(fields)
    return StartupTaskProposalRequest(**fields, request_hash=_hash(fields))


def evaluate_windows_startup_task_proposal(
    request: Any,
) -> StartupTaskProposalDecision:
    item = _validated_request(request)
    if not item.complete:
        status: ProposalStatus = "INCOMPLETE"
        reasons = ["STARTUP_TASK_PROPOSAL_INCOMPLETE"]
    else:
        reasons = []
        if item.trigger != ALLOWLISTED_TRIGGER:
            reasons.append("STARTUP_TASK_TRIGGER_NOT_ALLOWLISTED")
        if item.run_with_highest_privileges:
            reasons.append("STARTUP_TASK_HIGHEST_PRIVILEGES_REFUSED")
        if item.network_required:
            reasons.append("STARTUP_TASK_NETWORK_DEPENDENCY_REFUSED")
        if item.activation_requested:
            reasons.append("STARTUP_TASK_ACTIVATION_REFUSED")
        if not item.dry_run:
            reasons.append("STARTUP_TASK_DRY_RUN_REQUIRED")
        status = "DENIED" if reasons else "READY"
    preview = {
        "task_name": item.task_name,
        "trigger": ALLOWLISTED_TRIGGER,
        "supervisor_artifact_hash": item.supervisor_artifact_hash,
        "configuration_hash": item.configuration_hash,
        "run_with_highest_privileges": False,
        "network_required": False,
        "activation_disabled": True,
    }
    rollback = {
        "task_name": item.task_name,
        "rollback_script_hash": item.rollback_script_hash,
        "operation": "DELETE_TASK_IF_OPERATOR_APPROVED",
        "execution_disabled": True,
    }
    ready = status == "READY"
    unsigned = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "request_hash": item.request_hash,
        "task_name_hash": _hash(item.task_name),
        "trigger": ALLOWLISTED_TRIGGER,
        "configuration_preview_hash": _hash(preview),
        "rollback_preview_hash": _hash(rollback),
        "read_only": True,
        "proposal_ready": ready,
        "activation_disabled": True,
        "operator_review_required": True,
        "task_creation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return StartupTaskProposalDecision(
        status=status,
        reasons=tuple(reasons),
        request_hash=item.request_hash,
        task_name_hash=unsigned["task_name_hash"],
        trigger=ALLOWLISTED_TRIGGER,
        configuration_preview_hash=unsigned["configuration_preview_hash"],
        rollback_preview_hash=unsigned["rollback_preview_hash"],
        decision_hash=_hash(unsigned),
        proposal_ready=ready,
    )


def validate_startup_task_proposal_decision(value: Any) -> None:
    if not isinstance(value, StartupTaskProposalDecision):
        raise WindowsStartupTaskProposalError("STARTUP_TASK_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.proposal_ready != (value.status == "READY")
        or value.activation_disabled is not True
        or value.operator_review_required is not True
        or any(
            (
                value.task_creation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise WindowsStartupTaskProposalError("STARTUP_TASK_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = PROPOSAL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise WindowsStartupTaskProposalError("STARTUP_TASK_DECISION_HASH_MISMATCH")


def _validated_request(value: Any) -> StartupTaskProposalRequest:
    if not isinstance(value, StartupTaskProposalRequest):
        raise WindowsStartupTaskProposalError("STARTUP_TASK_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise WindowsStartupTaskProposalError("STARTUP_TASK_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "proposal_id_hash",
        "supervisor_artifact_hash",
        "configuration_hash",
        "rollback_script_hash",
        "task_name",
        "trigger",
        "run_with_highest_privileges",
        "network_required",
        "activation_requested",
        "dry_run",
        "complete",
    }
    if set(fields) != required:
        raise WindowsStartupTaskProposalError("STARTUP_TASK_REQUEST_FIELD_INVALID")
    for key in (
        "proposal_id_hash",
        "supervisor_artifact_hash",
        "configuration_hash",
        "rollback_script_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise WindowsStartupTaskProposalError("STARTUP_TASK_REQUEST_FIELD_INVALID")
    if (
        not isinstance(fields["task_name"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}", fields["task_name"]) is None
        or not isinstance(fields["trigger"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", fields["trigger"]) is None
    ):
        raise WindowsStartupTaskProposalError("STARTUP_TASK_REQUEST_FIELD_INVALID")
    for key in (
        "run_with_highest_privileges",
        "network_required",
        "activation_requested",
        "dry_run",
        "complete",
    ):
        if not isinstance(fields[key], bool):
            raise WindowsStartupTaskProposalError("STARTUP_TASK_REQUEST_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
