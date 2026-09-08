from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alert_loss_failure_injection import (
    AlertLossFailureInjectionError,
    evaluate_alert_loss_failure_injection,
    make_alert_loss_injection_evidence,
    validate_alert_loss_injection_result,
)


def _evidence(**overrides):
    fields = dict(
        incident_hash="1" * 64,
        alert_payload_hash="2" * 64,
        primary_channel_hash="3" * 64,
        secondary_channel_hash="4" * 64,
        primary_delivery_suppressed=True,
        primary_acknowledgement_observed=False,
        secondary_escalation_generated=True,
        recovery_progressed=False,
        restart_progressed=False,
        complete=True,
    )
    fields.update(overrides)
    return make_alert_loss_injection_evidence(**fields)


def test_lost_primary_alert_escalates_and_blocks_progression() -> None:
    first = evaluate_alert_loss_failure_injection(_evidence())
    assert first == evaluate_alert_loss_failure_injection(_evidence())
    assert first.status == "PASSED" and first.alert_loss_injected
    assert first.escalation_proven and first.fail_closed_proven
    assert not any(
        (first.recovery_authorized, first.restart_authorized, first.alert_delivery_authorized)
    )
    validate_alert_loss_injection_result(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"secondary_escalation_generated": False},
        {"recovery_progressed": True},
        {"restart_progressed": True},
    ],
)
def test_missing_escalation_or_unsafe_progression_fails(overrides) -> None:
    assert evaluate_alert_loss_failure_injection(_evidence(**overrides)).status == "FAILED"


def test_absent_injection_incomplete_capture_and_impossible_ack_fail_closed() -> None:
    assert (
        evaluate_alert_loss_failure_injection(_evidence(primary_delivery_suppressed=False)).status
        == "INCOMPLETE"
    )
    assert evaluate_alert_loss_failure_injection(_evidence(complete=False)).status == "INCOMPLETE"
    assert (
        evaluate_alert_loss_failure_injection(
            _evidence(primary_acknowledgement_observed=True)
        ).status
        == "TAMPERED"
    )


def test_evidence_and_result_tampering_fail_closed() -> None:
    with pytest.raises(AlertLossFailureInjectionError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_alert_loss_failure_injection(replace(_evidence(), restart_progressed=True))
    result = evaluate_alert_loss_failure_injection(_evidence())
    with pytest.raises(AlertLossFailureInjectionError, match="RESULT_HASH_MISMATCH"):
        validate_alert_loss_injection_result(replace(result, alert_loss_injected=False))
    with pytest.raises(AlertLossFailureInjectionError, match="SAFETY_BOUNDARY"):
        validate_alert_loss_injection_result(replace(result, restart_authorized=True))


def test_injection_evaluator_has_no_delivery_or_operational_surface() -> None:
    forbidden = {
        "open",
        "write",
        "send",
        "toast",
        "request",
        "run",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
    }
    assert forbidden.isdisjoint(evaluate_alert_loss_failure_injection.__code__.co_names)
