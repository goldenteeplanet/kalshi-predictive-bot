from dataclasses import replace

import pytest
from kalshi_predictor.workstation.failed_post_boot_automation_disablement import (
    FailedPostBootAutomationDisablementError,
    evaluate_failed_post_boot_automation_disablement,
    validate_automation_disablement_decision,
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


def _evaluate(notification=None, **overrides):
    fields = dict(
        startup_task_identity_hash="a" * 64,
        supervisor_identity_hash="b" * 64,
        target_identities_verified=True,
    )
    fields.update(overrides)
    return evaluate_failed_post_boot_automation_disablement(
        notification or _notification(), **fields
    )


def test_recovered_outcome_requires_no_disablement() -> None:
    first = _evaluate()
    assert first == _evaluate() and first.status == "NOT_REQUIRED"
    assert not first.disablement_required and not first.operator_action_required
    validate_automation_disablement_decision(first)


@pytest.mark.parametrize(
    "notification",
    [
        _notification(ui_verified=False),
        _notification(database_verified=False),
        _notification(evidence_complete=False),
    ],
)
def test_non_recovered_outcome_requires_operator_disablement(notification) -> None:
    result = _evaluate(notification)
    assert result.status == "REQUIRED" and result.disablement_required
    assert result.operator_action_required and not result.automation_disable_authorized


def test_unverified_target_identities_block_disablement() -> None:
    result = _evaluate(target_identities_verified=False)
    assert result.status == "INCOMPLETE" and result.operator_action_required
    assert not result.disablement_required and not result.automation_disable_authorized


def test_tampered_notification_and_fields_fail_closed() -> None:
    with pytest.raises(FailedPostBootAutomationDisablementError, match="NOTIFICATION_INVALID"):
        _evaluate(replace(_notification(), outcome="FAILED"))
    with pytest.raises(FailedPostBootAutomationDisablementError, match="FIELD_INVALID"):
        _evaluate(supervisor_identity_hash="bad")


def test_decision_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = _evaluate(_notification(database_verified=False))
    with pytest.raises(FailedPostBootAutomationDisablementError, match="HASH_MISMATCH"):
        validate_automation_disablement_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(FailedPostBootAutomationDisablementError, match="SAFETY_BOUNDARY"):
        validate_automation_disablement_decision(
            replace(decision, automation_disable_authorized=True)
        )
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "schtasks",
        "systemctl",
        "disable",
        "stop",
        "restart",
        "shutdown",
    }
    assert forbidden.isdisjoint(evaluate_failed_post_boot_automation_disablement.__code__.co_names)
