from dataclasses import replace

import pytest

from kalshi_predictor.workstation.five_minute_restart_warning import (
    WARNING_SECONDS,
    FiveMinuteRestartWarningError,
    evaluate_five_minute_restart_warning,
    make_restart_warning_request,
    validate_restart_warning_decision,
)


def _request(**overrides):
    fields = dict(
        incident_id_hash="1" * 64,
        eligibility_decision_hash="2" * 64,
        cancellation_command_hash="3" * 64,
        reason_summary_hash="4" * 64,
        eligibility_status="ELIGIBLE",
        issued_at_epoch=1_800_000_000,
        warning_seconds=WARNING_SECONDS,
        cancellation_command_present=True,
        operator_visible_preview=True,
        complete=True,
    )
    fields.update(overrides)
    return make_restart_warning_request(**fields)


def test_exact_five_minute_warning_is_deterministic_preview_only() -> None:
    first = evaluate_five_minute_restart_warning(_request())
    second = evaluate_five_minute_restart_warning(_request())
    assert first == second and first.status == "READY" and first.warning_preview_ready
    assert first.warning_seconds == 300
    assert first.warning_ends_at_epoch - first.warning_starts_at_epoch == 300
    assert first.cancellation_required
    assert not any(
        (
            first.notification_authorized,
            first.restart_authorized,
            first.service_control_authorized,
            first.execution_authorized,
        )
    )
    validate_restart_warning_decision(first)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"eligibility_status": "DENIED"}, "ELIGIBILITY_DENIED"),
        ({"warning_seconds": 299}, "DURATION_INVALID"),
        ({"warning_seconds": 301}, "DURATION_INVALID"),
        ({"cancellation_command_present": False}, "CANCELLATION_COMMAND_MISSING"),
        ({"operator_visible_preview": False}, "OPERATOR_PREVIEW_MISSING"),
    ],
)
def test_invalid_preconditions_deny_warning_readiness(overrides, reason) -> None:
    result = evaluate_five_minute_restart_warning(_request(**overrides))
    assert result.status == "DENIED" and not result.warning_preview_ready
    assert any(reason in item for item in result.reasons)


def test_incomplete_malformed_and_tampered_requests_fail_closed() -> None:
    assert evaluate_five_minute_restart_warning(_request(complete=False)).status == "INCOMPLETE"
    malformed = {key: value for key, value in _request().__dict__.items() if key != "request_hash"}
    malformed["issued_at_epoch"] = -1
    with pytest.raises(FiveMinuteRestartWarningError, match="FIELD_INVALID"):
        make_restart_warning_request(**malformed)
    with pytest.raises(FiveMinuteRestartWarningError, match="REQUEST_HASH_MISMATCH"):
        evaluate_five_minute_restart_warning(replace(_request(), warning_seconds=1))


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = evaluate_five_minute_restart_warning(_request())
    with pytest.raises(FiveMinuteRestartWarningError, match="DECISION_HASH_MISMATCH"):
        validate_restart_warning_decision(replace(decision, preview_hash="f" * 64))
    with pytest.raises(FiveMinuteRestartWarningError, match="SAFETY_BOUNDARY"):
        validate_restart_warning_decision(replace(decision, notification_authorized=True))
    with pytest.raises(FiveMinuteRestartWarningError, match="SAFETY_BOUNDARY"):
        validate_restart_warning_decision(replace(decision, restart_authorized=True))


def test_warning_model_has_no_operational_surfaces() -> None:
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "shutdown",
        "systemctl",
        "toast",
    }
    assert forbidden.isdisjoint(evaluate_five_minute_restart_warning.__code__.co_names)
