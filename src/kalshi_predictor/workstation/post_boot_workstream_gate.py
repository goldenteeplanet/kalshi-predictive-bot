from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .failed_post_boot_automation_disablement import (
    AutomationDisablementDecision,
    validate_automation_disablement_decision,
)
from .operator_recovery_handoff_packet import (
    OperatorRecoveryHandoffPacket,
    validate_operator_recovery_handoff_packet,
)
from .recovery_outcome_notification import (
    RecoveryOutcomeNotification,
    validate_recovery_outcome_notification,
)

GATE_SCHEMA_VERSION = "phase4km-post-boot-workstream-gate-v1"
REQUIRED_PHASES = ("4KD", "4KE", "4KF", "4KG", "4KH", "4KI", "4KJ", "4KK", "4KL")
GateStatus = Literal["PASSED", "DENIED", "INCOMPLETE"]


class PostBootWorkstreamGateError(ValueError):
    """Stable fail-closed post-boot workstream gate error."""


@dataclass(frozen=True)
class PostBootWorkstreamGateResult:
    status: GateStatus
    reasons: tuple[str, ...]
    outcome: str
    notification_hash: str
    disablement_decision_hash: str
    handoff_packet_hash: str
    covered_phases: tuple[str, ...]
    gate_hash: str
    read_only: bool = True
    workstream_integrity_proven: bool = False
    recovery_success_proven: bool = False
    operator_handoff_required: bool = True
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def evaluate_post_boot_workstream_gate(
    notification: Any, disablement: Any, handoff: Any, *, covered_phases: tuple[str, ...]
) -> PostBootWorkstreamGateResult:
    if (
        not isinstance(notification, RecoveryOutcomeNotification)
        or not isinstance(disablement, AutomationDisablementDecision)
        or not isinstance(handoff, OperatorRecoveryHandoffPacket)
    ):
        raise PostBootWorkstreamGateError("POST_BOOT_WORKSTREAM_INPUT_TYPE_INVALID")
    try:
        validate_recovery_outcome_notification(notification)
        validate_automation_disablement_decision(disablement)
        validate_operator_recovery_handoff_packet(handoff)
    except ValueError as exc:
        raise PostBootWorkstreamGateError("POST_BOOT_WORKSTREAM_INPUT_INVALID") from exc
    if not isinstance(covered_phases, tuple) or any(
        not isinstance(item, str) for item in covered_phases
    ):
        raise PostBootWorkstreamGateError("POST_BOOT_WORKSTREAM_PHASE_COVERAGE_INVALID")

    reasons = []
    if covered_phases != REQUIRED_PHASES:
        reasons.append("POST_BOOT_PHASE_COVERAGE_INCOMPLETE")
    if disablement.recovery_notification_hash != notification.notification_hash:
        reasons.append("POST_BOOT_DISABLEMENT_CHAIN_MISMATCH")
    if (
        handoff.notification_hash != notification.notification_hash
        or handoff.disablement_decision_hash != disablement.decision_hash
    ):
        reasons.append("POST_BOOT_HANDOFF_CHAIN_MISMATCH")
    if (
        handoff.restart_intent_hash != notification.restart_intent_hash
        or handoff.outcome != notification.outcome
    ):
        reasons.append("POST_BOOT_OUTCOME_CHAIN_MISMATCH")
    if notification.outcome != "RECOVERED" and disablement.status != "REQUIRED":
        reasons.append("POST_BOOT_FAILED_OUTCOME_NOT_QUARANTINED")
    if notification.outcome == "RECOVERED" and disablement.status != "NOT_REQUIRED":
        reasons.append("POST_BOOT_RECOVERED_OUTCOME_INCOHERENT")
    status: GateStatus = (
        "PASSED"
        if not reasons
        else "INCOMPLETE"
        if "POST_BOOT_PHASE_COVERAGE_INCOMPLETE" in reasons
        else "DENIED"
    )
    integrity = status == "PASSED"
    recovery_success = integrity and notification.outcome == "RECOVERED"
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "outcome": notification.outcome,
        "notification_hash": notification.notification_hash,
        "disablement_decision_hash": disablement.decision_hash,
        "handoff_packet_hash": handoff.packet_hash,
        "covered_phases": list(covered_phases),
        "read_only": True,
        "workstream_integrity_proven": integrity,
        "recovery_success_proven": recovery_success,
        "operator_handoff_required": True,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return PostBootWorkstreamGateResult(
        status=status,
        reasons=tuple(reasons),
        outcome=notification.outcome,
        notification_hash=notification.notification_hash,
        disablement_decision_hash=disablement.decision_hash,
        handoff_packet_hash=handoff.packet_hash,
        covered_phases=covered_phases,
        gate_hash=_hash(unsigned),
        workstream_integrity_proven=integrity,
        recovery_success_proven=recovery_success,
    )


def validate_post_boot_workstream_gate_result(value: Any) -> None:
    if not isinstance(value, PostBootWorkstreamGateResult):
        raise PostBootWorkstreamGateError("POST_BOOT_WORKSTREAM_RESULT_TYPE_INVALID")
    passed = value.status == "PASSED"
    if (
        value.read_only is not True
        or value.operator_handoff_required is not True
        or value.workstream_integrity_proven != passed
        or value.recovery_success_proven != (passed and value.outcome == "RECOVERED")
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise PostBootWorkstreamGateError("POST_BOOT_WORKSTREAM_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("gate_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["covered_phases"] = list(unsigned["covered_phases"])
    if value.gate_hash != _hash(unsigned):
        raise PostBootWorkstreamGateError("POST_BOOT_WORKSTREAM_GATE_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
