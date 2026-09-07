from dataclasses import replace

import pytest

from kalshi_predictor.workstation.restart_loop_circuit_breaker import (
    RestartLoopCircuitBreakerError,
    evaluate_restart_loop_circuit_breaker,
    make_restart_loop_state,
    validate_restart_loop_decision,
)


def _state(**overrides):
    fields = dict(
        incident_id_hash="1" * 64,
        history_file_hash="2" * 64,
        latest_intent_hash="3" * 64,
        restart_attempts_for_incident=0,
        post_boot_status="NONE",
        automatic_recovery_disabled=False,
        operator_intervention_required=False,
        state_complete=True,
        integrity_verified=True,
    )
    fields.update(overrides)
    return make_restart_loop_state(**fields)


def test_clean_never_restarted_incident_closes_breaker_without_restart_authority() -> None:
    first = evaluate_restart_loop_circuit_breaker(_state())
    assert first == evaluate_restart_loop_circuit_breaker(_state())
    assert first.status == "CLOSED" and first.circuit_closed
    assert first.automatic_recovery_allowed and not first.operator_intervention_required
    assert not first.restart_authorized
    validate_restart_loop_decision(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"restart_attempts_for_incident": 1, "post_boot_status": "PENDING"},
        {"restart_attempts_for_incident": 1, "post_boot_status": "PASSED"},
        {
            "restart_attempts_for_incident": 1,
            "post_boot_status": "FAILED",
            "automatic_recovery_disabled": True,
            "operator_intervention_required": True,
        },
        {"automatic_recovery_disabled": True},
        {"operator_intervention_required": True},
    ],
)
def test_attempt_pending_failed_disabled_or_operator_state_opens_breaker(overrides) -> None:
    result = evaluate_restart_loop_circuit_breaker(_state(**overrides))
    assert result.status == "OPEN" and not result.circuit_closed
    assert result.operator_intervention_required and not result.restart_authorized


@pytest.mark.parametrize(
    "overrides",
    [
        {"restart_attempts_for_incident": 1, "post_boot_status": "NONE"},
        {"restart_attempts_for_incident": 0, "post_boot_status": "PENDING"},
        {"restart_attempts_for_incident": 1, "post_boot_status": "FAILED"},
    ],
)
def test_contradictory_state_is_tampered(overrides) -> None:
    assert evaluate_restart_loop_circuit_breaker(_state(**overrides)).status == "TAMPERED"


def test_incomplete_or_unverified_state_denies() -> None:
    assert evaluate_restart_loop_circuit_breaker(_state(state_complete=False)).status == "DENIED"
    assert (
        evaluate_restart_loop_circuit_breaker(_state(integrity_verified=False)).status == "DENIED"
    )


def test_malformed_state_and_tampering_fail_closed() -> None:
    with pytest.raises(RestartLoopCircuitBreakerError, match="FIELD_INVALID"):
        _state(restart_attempts_for_incident=2)
    state = _state()
    with pytest.raises(RestartLoopCircuitBreakerError, match="STATE_HASH_MISMATCH"):
        evaluate_restart_loop_circuit_breaker(replace(state, operator_intervention_required=True))
    decision = evaluate_restart_loop_circuit_breaker(state)
    with pytest.raises(RestartLoopCircuitBreakerError, match="DECISION_HASH_MISMATCH"):
        validate_restart_loop_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(RestartLoopCircuitBreakerError, match="SAFETY_BOUNDARY"):
        validate_restart_loop_decision(replace(decision, restart_authorized=True))


def test_breaker_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "popen", "subprocess", "socket", "shutdown", "systemctl"}
    assert forbidden.isdisjoint(evaluate_restart_loop_circuit_breaker.__code__.co_names)
