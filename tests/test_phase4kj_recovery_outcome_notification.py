from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_outcome_notification import (
    RecoveryOutcomeNotificationError,
    build_recovery_outcome_notification,
    validate_recovery_outcome_notification,
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
        created_at_epoch_seconds=1_000,
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


def test_recovered_notification_is_deterministic_and_non_authorizing() -> None:
    first = _notification()
    assert first == _notification() and first.outcome == "RECOVERED" and first.severity == "INFO"
    assert first.operator_notification_required and not first.delivery_authorized
    assert not any(
        (first.recovery_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_recovery_outcome_notification(first)


def test_ui_only_failure_is_degraded_warning() -> None:
    result = _notification(ui_verified=False)
    assert result.outcome == "DEGRADED" and result.severity == "WARNING"
    assert result.reason_codes == ("POST_BOOT_UI_NOT_VERIFIED",)


@pytest.mark.parametrize(
    "field",
    [
        "wsl_verified",
        "scheduler_verified",
        "database_verified",
        "invariants_verified",
        "writer_verified",
    ],
)
def test_safety_check_failure_is_critical(field) -> None:
    result = _notification(**{field: False})
    assert result.outcome == "FAILED" and result.severity == "CRITICAL"


def test_incomplete_evidence_is_critical_and_lists_failed_checks() -> None:
    result = _notification(evidence_complete=False, database_verified=False)
    assert result.outcome == "INCOMPLETE" and result.severity == "CRITICAL"
    assert result.reason_codes[0] == "POST_BOOT_EVIDENCE_INCOMPLETE"
    assert "POST_BOOT_DATABASE_NOT_VERIFIED" in result.reason_codes


def test_invalid_fields_and_notification_tampering_fail_closed() -> None:
    with pytest.raises(RecoveryOutcomeNotificationError, match="FIELD_INVALID"):
        _notification(ui_decision_hash="bad")
    result = _notification()
    with pytest.raises(RecoveryOutcomeNotificationError, match="HASH_MISMATCH"):
        validate_recovery_outcome_notification(replace(result, body="forged"))
    with pytest.raises(RecoveryOutcomeNotificationError, match="SAFETY_BOUNDARY"):
        validate_recovery_outcome_notification(replace(result, delivery_authorized=True))


def test_builder_has_no_delivery_service_restart_or_execution_surface() -> None:
    forbidden = {
        "open",
        "write",
        "send",
        "toast",
        "request",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
        "order",
    }
    assert forbidden.isdisjoint(build_recovery_outcome_notification.__code__.co_names)
