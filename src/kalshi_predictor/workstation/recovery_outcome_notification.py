from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

NOTIFICATION_SCHEMA_VERSION = "phase4kj-recovery-outcome-notification-v1"
Outcome = Literal["RECOVERED", "DEGRADED", "FAILED", "INCOMPLETE"]


class RecoveryOutcomeNotificationError(ValueError):
    """Stable fail-closed recovery-outcome notification error."""


@dataclass(frozen=True)
class RecoveryOutcomeNotification:
    outcome: Outcome
    severity: str
    reason_codes: tuple[str, ...]
    restart_intent_hash: str
    wsl_decision_hash: str
    scheduler_decision_hash: str
    database_decision_hash: str
    invariant_decision_hash: str
    writer_decision_hash: str
    ui_decision_hash: str
    created_at_epoch_seconds: int
    title: str
    body: str
    notification_hash: str
    operator_notification_required: bool = True
    delivery_authorized: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def build_recovery_outcome_notification(
    *,
    restart_intent_hash: str,
    wsl_decision_hash: str,
    scheduler_decision_hash: str,
    database_decision_hash: str,
    invariant_decision_hash: str,
    writer_decision_hash: str,
    ui_decision_hash: str,
    created_at_epoch_seconds: int,
    wsl_verified: bool,
    scheduler_verified: bool,
    database_verified: bool,
    invariants_verified: bool,
    writer_verified: bool,
    ui_verified: bool,
    evidence_complete: bool,
) -> RecoveryOutcomeNotification:
    hashes = {
        "restart_intent_hash": restart_intent_hash,
        "wsl_decision_hash": wsl_decision_hash,
        "scheduler_decision_hash": scheduler_decision_hash,
        "database_decision_hash": database_decision_hash,
        "invariant_decision_hash": invariant_decision_hash,
        "writer_decision_hash": writer_decision_hash,
        "ui_decision_hash": ui_decision_hash,
    }
    for value in hashes.values():
        _require_hash(value)
    if (
        isinstance(created_at_epoch_seconds, bool)
        or not isinstance(created_at_epoch_seconds, int)
        or created_at_epoch_seconds < 0
    ):
        raise RecoveryOutcomeNotificationError("RECOVERY_NOTIFICATION_FIELD_INVALID")
    checks = {
        "WSL": wsl_verified,
        "SCHEDULER": scheduler_verified,
        "DATABASE": database_verified,
        "INVARIANTS": invariants_verified,
        "WRITER_EXCLUSIVITY": writer_verified,
        "UI": ui_verified,
    }
    if any(not isinstance(value, bool) for value in (*checks.values(), evidence_complete)):
        raise RecoveryOutcomeNotificationError("RECOVERY_NOTIFICATION_FIELD_INVALID")

    failed = [f"POST_BOOT_{name}_NOT_VERIFIED" for name, passed in checks.items() if not passed]
    if not evidence_complete:
        outcome: Outcome = "INCOMPLETE"
        severity = "CRITICAL"
        reasons = ["POST_BOOT_EVIDENCE_INCOMPLETE", *failed]
    elif not failed:
        outcome = "RECOVERED"
        severity = "INFO"
        reasons = []
    elif len(failed) == 1 and failed[0] == "POST_BOOT_UI_NOT_VERIFIED":
        outcome = "DEGRADED"
        severity = "WARNING"
        reasons = failed
    else:
        outcome = "FAILED"
        severity = "CRITICAL"
        reasons = failed
    title = f"Kalshi recovery outcome: {outcome}"
    body = "All post-boot checks verified." if not reasons else "; ".join(reasons)
    unsigned = {
        "schema_version": NOTIFICATION_SCHEMA_VERSION,
        "outcome": outcome,
        "severity": severity,
        "reason_codes": reasons,
        **hashes,
        "created_at_epoch_seconds": created_at_epoch_seconds,
        "title": title,
        "body": body,
        "operator_notification_required": True,
        "delivery_authorized": False,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RecoveryOutcomeNotification(
        outcome=outcome,
        severity=severity,
        reason_codes=tuple(reasons),
        restart_intent_hash=restart_intent_hash,
        wsl_decision_hash=wsl_decision_hash,
        scheduler_decision_hash=scheduler_decision_hash,
        database_decision_hash=database_decision_hash,
        invariant_decision_hash=invariant_decision_hash,
        writer_decision_hash=writer_decision_hash,
        ui_decision_hash=ui_decision_hash,
        created_at_epoch_seconds=created_at_epoch_seconds,
        title=title,
        body=body,
        notification_hash=_hash(unsigned),
    )


def validate_recovery_outcome_notification(value: Any) -> None:
    if not isinstance(value, RecoveryOutcomeNotification):
        raise RecoveryOutcomeNotificationError("RECOVERY_NOTIFICATION_TYPE_INVALID")
    if value.operator_notification_required is not True or any(
        (
            value.delivery_authorized,
            value.recovery_authorized,
            value.restart_authorized,
            value.service_control_authorized,
            value.execution_authorized,
        )
    ):
        raise RecoveryOutcomeNotificationError("RECOVERY_NOTIFICATION_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("notification_hash")
    unsigned["schema_version"] = NOTIFICATION_SCHEMA_VERSION
    unsigned["reason_codes"] = list(unsigned["reason_codes"])
    if value.notification_hash != _hash(unsigned):
        raise RecoveryOutcomeNotificationError("RECOVERY_NOTIFICATION_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RecoveryOutcomeNotificationError("RECOVERY_NOTIFICATION_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
