from dataclasses import replace

import pytest

from kalshi_predictor.workstation.failed_post_boot_automation_disablement import (
    evaluate_failed_post_boot_automation_disablement,
)
from kalshi_predictor.workstation.operator_recovery_handoff_packet import (
    OperatorRecoveryHandoffError,
    build_operator_recovery_handoff_packet,
    validate_operator_recovery_handoff_packet,
)
from kalshi_predictor.workstation.recovery_outcome_notification import (
    build_recovery_outcome_notification,
)


def _notification(**overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        wsl_decision_hash="2" * 64,
        scheduler_decision_hash="3" * 64,
        database_decision_hash="4" * 64,
        invariant_decision_hash="5" * 64,
        writer_decision_hash="6" * 64,
        ui_decision_hash="7" * 64,
        created_at_epoch_seconds=100,
        wsl_verified=True,
        scheduler_verified=True,
        database_verified=True,
        invariants_verified=True,
        writer_verified=True,
        ui_verified=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    return build_recovery_outcome_notification(**fields)


def _disablement(notification, *, identities=True):
    return evaluate_failed_post_boot_automation_disablement(
        notification,
        startup_task_identity_hash="a" * 64,
        supervisor_identity_hash="b" * 64,
        target_identities_verified=identities,
    )


def test_recovered_handoff_is_deterministic_and_requires_review() -> None:
    notification = _notification()
    first = build_operator_recovery_handoff_packet(notification, _disablement(notification))
    second = build_operator_recovery_handoff_packet(notification, _disablement(notification))
    assert first == second and first.outcome == "RECOVERED"
    assert first.required_actions == ("ACKNOWLEDGE_RECOVERY_OUTCOME", "REVIEW_POST_BOOT_EVIDENCE")
    assert first.operator_review_required and first.acknowledgement_required
    validate_operator_recovery_handoff_packet(first)


def test_failed_recovery_handoff_requires_manual_disablement_and_investigation() -> None:
    notification = _notification(database_verified=False)
    packet = build_operator_recovery_handoff_packet(notification, _disablement(notification))
    assert "DISABLE_RECOVERY_AUTOMATION_MANUALLY" in packet.required_actions
    assert "INVESTIGATE_FAILED_POST_BOOT_CHECKS" in packet.required_actions
    assert not packet.automation_disable_authorized


def test_unverified_targets_require_quarantine_and_identity_review() -> None:
    notification = _notification(database_verified=False)
    packet = build_operator_recovery_handoff_packet(
        notification, _disablement(notification, identities=False)
    )
    assert "VERIFY_AUTOMATION_TARGET_IDENTITIES" in packet.required_actions
    assert "KEEP_RECOVERY_AUTOMATION_QUARANTINED" in packet.required_actions


def test_mismatched_or_tampered_upstream_evidence_fails_closed() -> None:
    first = _notification()
    second = _notification(ui_verified=False)
    with pytest.raises(OperatorRecoveryHandoffError, match="CHAIN_MISMATCH"):
        build_operator_recovery_handoff_packet(first, _disablement(second))
    with pytest.raises(OperatorRecoveryHandoffError, match="UPSTREAM_EVIDENCE_INVALID"):
        build_operator_recovery_handoff_packet(
            first, replace(_disablement(first), status="REQUIRED")
        )


def test_packet_tampering_and_operational_surfaces_fail_closed() -> None:
    notification = _notification()
    packet = build_operator_recovery_handoff_packet(notification, _disablement(notification))
    with pytest.raises(OperatorRecoveryHandoffError, match="HASH_MISMATCH"):
        validate_operator_recovery_handoff_packet(replace(packet, severity="CRITICAL"))
    with pytest.raises(OperatorRecoveryHandoffError, match="SAFETY_BOUNDARY"):
        validate_operator_recovery_handoff_packet(replace(packet, recovery_authorized=True))
    forbidden = {
        "open",
        "write",
        "send",
        "run",
        "Popen",
        "subprocess",
        "schtasks",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
    }
    assert forbidden.isdisjoint(build_operator_recovery_handoff_packet.__code__.co_names)
