from dataclasses import replace

import pytest
from kalshi_predictor.workstation.startup_ordering_delay_model import (
    REQUIRED_STAGES,
    STARTUP_DELAY_SECONDS,
    StartupOrderingDelayModelError,
    evaluate_startup_ordering_delay,
    make_startup_ordering_request,
    validate_startup_ordering_decision,
)


def _request(**overrides):
    fields = dict(
        proposal_hash="1" * 64,
        identity_audit_hash="2" * 64,
        configuration_hash="3" * 64,
        stages=REQUIRED_STAGES,
        startup_delay_seconds=STARTUP_DELAY_SECONDS,
        recovery_on_boot_requested=False,
        activation_requested=False,
        complete=True,
    )
    fields.update(overrides)
    return make_startup_ordering_request(**fields)


def test_exact_delayed_alert_only_startup_order_is_valid_non_authorizing() -> None:
    first = evaluate_startup_ordering_delay(_request())
    assert first == evaluate_startup_ordering_delay(_request())
    assert first.status == "VALID" and first.startup_model_valid
    assert first.startup_delay_seconds == 120 and first.stages == REQUIRED_STAGES
    assert first.alert_only_after_boot
    assert not any(
        (first.activation_authorized, first.recovery_authorized, first.restart_authorized)
    )
    validate_startup_ordering_decision(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"stages": tuple(reversed(REQUIRED_STAGES))},
        {"stages": REQUIRED_STAGES[:-1]},
        {"startup_delay_seconds": 119},
        {"startup_delay_seconds": 121},
        {"recovery_on_boot_requested": True},
        {"activation_requested": True},
    ],
)
def test_order_delay_recovery_or_activation_drift_is_denied(overrides) -> None:
    result = evaluate_startup_ordering_delay(_request(**overrides))
    assert result.status == "DENIED" and not result.startup_model_valid


def test_duplicate_unknown_incomplete_malformed_and_request_tampering_fail_closed() -> None:
    assert (
        evaluate_startup_ordering_delay(
            _request(stages=(*REQUIRED_STAGES, REQUIRED_STAGES[-1]))
        ).status
        == "TAMPERED"
    )
    assert (
        evaluate_startup_ordering_delay(
            _request(stages=(*REQUIRED_STAGES[:-1], "UNKNOWN_STAGE"))
        ).status
        == "TAMPERED"
    )
    assert evaluate_startup_ordering_delay(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(StartupOrderingDelayModelError, match="FIELD_INVALID"):
        _request(startup_delay_seconds=-1)
    with pytest.raises(StartupOrderingDelayModelError, match="REQUEST_HASH_MISMATCH"):
        evaluate_startup_ordering_delay(replace(_request(), activation_requested=True))


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = evaluate_startup_ordering_delay(_request())
    with pytest.raises(StartupOrderingDelayModelError, match="DECISION_HASH_MISMATCH"):
        validate_startup_ordering_decision(replace(decision, ordering_hash="f" * 64))
    with pytest.raises(StartupOrderingDelayModelError, match="SAFETY_BOUNDARY"):
        validate_startup_ordering_decision(replace(decision, recovery_authorized=True))


def test_model_has_no_clock_sleep_task_or_operational_surface() -> None:
    forbidden = {
        "sleep",
        "time",
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "spawn",
        "schtasks",
    }
    assert forbidden.isdisjoint(evaluate_startup_ordering_delay.__code__.co_names)
