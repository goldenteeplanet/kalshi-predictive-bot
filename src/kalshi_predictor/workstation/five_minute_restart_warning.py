from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

WARNING_SCHEMA_VERSION = "phase4jb-five-minute-restart-warning-v1"
WARNING_SECONDS = 300
WarningStatus = Literal["READY", "DENIED", "INCOMPLETE", "TAMPERED"]


class FiveMinuteRestartWarningError(ValueError):
    """Stable fail-closed five-minute restart warning error."""


@dataclass(frozen=True)
class RestartWarningRequest:
    incident_id_hash: str
    eligibility_decision_hash: str
    cancellation_command_hash: str
    reason_summary_hash: str
    eligibility_status: str
    issued_at_epoch: int
    warning_seconds: int
    cancellation_command_present: bool
    operator_visible_preview: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class RestartWarningDecision:
    status: WarningStatus
    reasons: tuple[str, ...]
    incident_id_hash: str
    request_hash: str
    warning_starts_at_epoch: int
    warning_ends_at_epoch: int
    warning_seconds: int
    preview_hash: str
    decision_hash: str
    read_only: bool = True
    warning_preview_ready: bool = False
    cancellation_required: bool = True
    notification_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_warning_request(**fields: Any) -> RestartWarningRequest:
    _validate_fields(fields)
    return RestartWarningRequest(**fields, request_hash=_hash(fields))


def evaluate_five_minute_restart_warning(request: Any) -> RestartWarningDecision:
    item = _validated_request(request)
    if not item.complete:
        status: WarningStatus = "INCOMPLETE"
        reasons = ["RESTART_WARNING_REQUEST_INCOMPLETE"]
    else:
        reasons = []
        if item.eligibility_status != "ELIGIBLE":
            reasons.append(f"RESTART_WARNING_ELIGIBILITY_DENIED:{item.eligibility_status}")
        if item.warning_seconds != WARNING_SECONDS:
            reasons.append("RESTART_WARNING_DURATION_INVALID")
        if not item.cancellation_command_present:
            reasons.append("RESTART_WARNING_CANCELLATION_COMMAND_MISSING")
        if not item.operator_visible_preview:
            reasons.append("RESTART_WARNING_OPERATOR_PREVIEW_MISSING")
        status = "DENIED" if reasons else "READY"
    ready = status == "READY"
    end_at = item.issued_at_epoch + WARNING_SECONDS
    preview = {
        "incident_id_hash": item.incident_id_hash,
        "eligibility_decision_hash": item.eligibility_decision_hash,
        "cancellation_command_hash": item.cancellation_command_hash,
        "reason_summary_hash": item.reason_summary_hash,
        "warning_starts_at_epoch": item.issued_at_epoch,
        "warning_ends_at_epoch": end_at,
        "warning_seconds": WARNING_SECONDS,
    }
    preview_hash = _hash(preview)
    unsigned = {
        "schema_version": WARNING_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "incident_id_hash": item.incident_id_hash,
        "request_hash": item.request_hash,
        "warning_starts_at_epoch": item.issued_at_epoch,
        "warning_ends_at_epoch": end_at,
        "warning_seconds": WARNING_SECONDS,
        "preview_hash": preview_hash,
        "read_only": True,
        "warning_preview_ready": ready,
        "cancellation_required": True,
        "notification_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartWarningDecision(
        status=status,
        reasons=tuple(reasons),
        incident_id_hash=item.incident_id_hash,
        request_hash=item.request_hash,
        warning_starts_at_epoch=item.issued_at_epoch,
        warning_ends_at_epoch=end_at,
        warning_seconds=WARNING_SECONDS,
        preview_hash=preview_hash,
        decision_hash=_hash(unsigned),
        warning_preview_ready=ready,
    )


def validate_restart_warning_decision(value: Any) -> None:
    if not isinstance(value, RestartWarningDecision):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.cancellation_required is not True
        or value.warning_seconds != WARNING_SECONDS
        or value.warning_ends_at_epoch - value.warning_starts_at_epoch != WARNING_SECONDS
        or any(
            (
                value.notification_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_SAFETY_BOUNDARY_INVALID")
    if value.warning_preview_ready != (value.status == "READY"):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = WARNING_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_DECISION_HASH_MISMATCH")


def _validated_request(value: Any) -> RestartWarningRequest:
    if not isinstance(value, RestartWarningRequest):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_id_hash",
        "eligibility_decision_hash",
        "cancellation_command_hash",
        "reason_summary_hash",
        "eligibility_status",
        "issued_at_epoch",
        "warning_seconds",
        "cancellation_command_present",
        "operator_visible_preview",
        "complete",
    }
    if set(fields) != required:
        raise FiveMinuteRestartWarningError("RESTART_WARNING_FIELD_INVALID")
    for key in (
        "incident_id_hash",
        "eligibility_decision_hash",
        "cancellation_command_hash",
        "reason_summary_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise FiveMinuteRestartWarningError("RESTART_WARNING_FIELD_INVALID")
    if (
        not isinstance(fields["eligibility_status"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["eligibility_status"]) is None
        or isinstance(fields["issued_at_epoch"], bool)
        or not isinstance(fields["issued_at_epoch"], int)
        or fields["issued_at_epoch"] < 0
        or isinstance(fields["warning_seconds"], bool)
        or not isinstance(fields["warning_seconds"], int)
        or fields["warning_seconds"] <= 0
    ):
        raise FiveMinuteRestartWarningError("RESTART_WARNING_FIELD_INVALID")
    for key in ("cancellation_command_present", "operator_visible_preview", "complete"):
        if not isinstance(fields[key], bool):
            raise FiveMinuteRestartWarningError("RESTART_WARNING_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
