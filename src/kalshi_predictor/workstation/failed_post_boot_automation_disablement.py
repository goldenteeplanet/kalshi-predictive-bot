from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .recovery_outcome_notification import (
    RecoveryOutcomeNotification,
    validate_recovery_outcome_notification,
)

DISABLEMENT_SCHEMA_VERSION = "phase4kk-failed-post-boot-automation-disablement-v1"
DisablementStatus = Literal["NOT_REQUIRED", "REQUIRED", "INCOMPLETE"]


class FailedPostBootAutomationDisablementError(ValueError):
    """Stable fail-closed post-boot automation-disablement error."""


@dataclass(frozen=True)
class AutomationDisablementDecision:
    status: DisablementStatus
    reasons: tuple[str, ...]
    recovery_notification_hash: str
    startup_task_identity_hash: str
    supervisor_identity_hash: str
    decision_hash: str
    read_only: bool = True
    disablement_required: bool = False
    operator_action_required: bool = False
    automation_disable_authorized: bool = False
    service_control_authorized: bool = False
    restart_authorized: bool = False
    execution_authorized: bool = False


def evaluate_failed_post_boot_automation_disablement(
    notification: Any,
    *,
    startup_task_identity_hash: str,
    supervisor_identity_hash: str,
    target_identities_verified: bool,
) -> AutomationDisablementDecision:
    _require_hash(startup_task_identity_hash)
    _require_hash(supervisor_identity_hash)
    if not isinstance(target_identities_verified, bool):
        raise FailedPostBootAutomationDisablementError("AUTOMATION_DISABLEMENT_FIELD_INVALID")
    if not isinstance(notification, RecoveryOutcomeNotification):
        raise FailedPostBootAutomationDisablementError("RECOVERY_NOTIFICATION_TYPE_INVALID")
    try:
        validate_recovery_outcome_notification(notification)
    except ValueError as exc:
        raise FailedPostBootAutomationDisablementError("RECOVERY_NOTIFICATION_INVALID") from exc

    if not target_identities_verified:
        status: DisablementStatus = "INCOMPLETE"
        reasons = ["AUTOMATION_TARGET_IDENTITIES_UNVERIFIED"]
    elif notification.outcome == "RECOVERED":
        status = "NOT_REQUIRED"
        reasons = []
    else:
        status = "REQUIRED"
        reasons = [f"POST_BOOT_OUTCOME_{notification.outcome}"]
    required = status == "REQUIRED"
    unsigned = {
        "schema_version": DISABLEMENT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "recovery_notification_hash": notification.notification_hash,
        "startup_task_identity_hash": startup_task_identity_hash,
        "supervisor_identity_hash": supervisor_identity_hash,
        "read_only": True,
        "disablement_required": required,
        "operator_action_required": status != "NOT_REQUIRED",
        "automation_disable_authorized": False,
        "service_control_authorized": False,
        "restart_authorized": False,
        "execution_authorized": False,
    }
    return AutomationDisablementDecision(
        status=status,
        reasons=tuple(reasons),
        recovery_notification_hash=notification.notification_hash,
        startup_task_identity_hash=startup_task_identity_hash,
        supervisor_identity_hash=supervisor_identity_hash,
        decision_hash=_hash(unsigned),
        disablement_required=required,
        operator_action_required=status != "NOT_REQUIRED",
    )


def validate_automation_disablement_decision(value: Any) -> None:
    if not isinstance(value, AutomationDisablementDecision):
        raise FailedPostBootAutomationDisablementError("AUTOMATION_DISABLEMENT_TYPE_INVALID")
    required = value.status == "REQUIRED"
    if (
        value.read_only is not True
        or value.disablement_required != required
        or value.operator_action_required != (value.status != "NOT_REQUIRED")
        or any(
            (
                value.automation_disable_authorized,
                value.service_control_authorized,
                value.restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise FailedPostBootAutomationDisablementError(
            "AUTOMATION_DISABLEMENT_SAFETY_BOUNDARY_INVALID"
        )
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = DISABLEMENT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise FailedPostBootAutomationDisablementError("AUTOMATION_DISABLEMENT_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise FailedPostBootAutomationDisablementError("AUTOMATION_DISABLEMENT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
