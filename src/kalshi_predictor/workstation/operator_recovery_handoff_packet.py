from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .failed_post_boot_automation_disablement import (
    AutomationDisablementDecision,
    validate_automation_disablement_decision,
)
from .recovery_outcome_notification import (
    RecoveryOutcomeNotification,
    validate_recovery_outcome_notification,
)

HANDOFF_SCHEMA_VERSION = "phase4kl-operator-recovery-handoff-packet-v1"


class OperatorRecoveryHandoffError(ValueError):
    """Stable fail-closed operator recovery handoff error."""


@dataclass(frozen=True)
class OperatorRecoveryHandoffPacket:
    outcome: str
    severity: str
    restart_intent_hash: str
    notification_hash: str
    disablement_decision_hash: str
    created_at_epoch_seconds: int
    required_actions: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    packet_hash: str
    operator_review_required: bool = True
    acknowledgement_required: bool = True
    read_only: bool = True
    automation_disable_authorized: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def build_operator_recovery_handoff_packet(
    notification: Any,
    disablement: Any,
) -> OperatorRecoveryHandoffPacket:
    if not isinstance(notification, RecoveryOutcomeNotification):
        raise OperatorRecoveryHandoffError("HANDOFF_NOTIFICATION_TYPE_INVALID")
    if not isinstance(disablement, AutomationDisablementDecision):
        raise OperatorRecoveryHandoffError("HANDOFF_DISABLEMENT_TYPE_INVALID")
    try:
        validate_recovery_outcome_notification(notification)
        validate_automation_disablement_decision(disablement)
    except ValueError as exc:
        raise OperatorRecoveryHandoffError("HANDOFF_UPSTREAM_EVIDENCE_INVALID") from exc
    if disablement.recovery_notification_hash != notification.notification_hash:
        raise OperatorRecoveryHandoffError("HANDOFF_UPSTREAM_CHAIN_MISMATCH")

    actions = ["ACKNOWLEDGE_RECOVERY_OUTCOME"]
    if disablement.status == "REQUIRED":
        actions.extend(
            ("DISABLE_RECOVERY_AUTOMATION_MANUALLY", "INVESTIGATE_FAILED_POST_BOOT_CHECKS")
        )
    elif disablement.status == "INCOMPLETE":
        actions.extend(
            ("VERIFY_AUTOMATION_TARGET_IDENTITIES", "KEEP_RECOVERY_AUTOMATION_QUARANTINED")
        )
    elif notification.outcome == "RECOVERED":
        actions.append("REVIEW_POST_BOOT_EVIDENCE")
    else:
        raise OperatorRecoveryHandoffError("HANDOFF_UPSTREAM_STATE_INCOHERENT")
    evidence_hashes = (
        notification.wsl_decision_hash,
        notification.scheduler_decision_hash,
        notification.database_decision_hash,
        notification.invariant_decision_hash,
        notification.writer_decision_hash,
        notification.ui_decision_hash,
    )
    unsigned = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "outcome": notification.outcome,
        "severity": notification.severity,
        "restart_intent_hash": notification.restart_intent_hash,
        "notification_hash": notification.notification_hash,
        "disablement_decision_hash": disablement.decision_hash,
        "created_at_epoch_seconds": notification.created_at_epoch_seconds,
        "required_actions": actions,
        "evidence_hashes": list(evidence_hashes),
        "operator_review_required": True,
        "acknowledgement_required": True,
        "read_only": True,
        "automation_disable_authorized": False,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return OperatorRecoveryHandoffPacket(
        outcome=notification.outcome,
        severity=notification.severity,
        restart_intent_hash=notification.restart_intent_hash,
        notification_hash=notification.notification_hash,
        disablement_decision_hash=disablement.decision_hash,
        created_at_epoch_seconds=notification.created_at_epoch_seconds,
        required_actions=tuple(actions),
        evidence_hashes=evidence_hashes,
        packet_hash=_hash(unsigned),
    )


def validate_operator_recovery_handoff_packet(value: Any) -> None:
    if not isinstance(value, OperatorRecoveryHandoffPacket):
        raise OperatorRecoveryHandoffError("HANDOFF_PACKET_TYPE_INVALID")
    if (
        value.operator_review_required is not True
        or value.acknowledgement_required is not True
        or value.read_only is not True
        or any(
            (
                value.automation_disable_authorized,
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise OperatorRecoveryHandoffError("HANDOFF_PACKET_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("packet_hash")
    unsigned["schema_version"] = HANDOFF_SCHEMA_VERSION
    unsigned["required_actions"] = list(unsigned["required_actions"])
    unsigned["evidence_hashes"] = list(unsigned["evidence_hashes"])
    if value.packet_hash != _hash(unsigned):
        raise OperatorRecoveryHandoffError("HANDOFF_PACKET_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
