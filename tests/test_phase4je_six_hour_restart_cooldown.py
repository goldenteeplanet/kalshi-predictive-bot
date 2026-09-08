from dataclasses import replace

import pytest

from kalshi_predictor.workstation.six_hour_restart_cooldown import (
    COOLDOWN_SECONDS,
    SixHourRestartCooldownError,
    evaluate_six_hour_restart_cooldown,
    make_restart_cooldown_state,
    validate_restart_cooldown_decision,
)


def _state(**overrides):
    fields = dict(
        state_id_hash="1" * 64,
        history_file_hash="2" * 64,
        latest_intent_hash="3" * 64,
        has_restart_history=True,
        last_restart_at_epoch=1_000,
        state_complete=True,
        integrity_verified=True,
    )
    fields.update(overrides)
    return make_restart_cooldown_state(**fields)


def test_six_hour_boundary_is_exact_deterministic_and_non_authorizing() -> None:
    before = evaluate_six_hour_restart_cooldown(
        _state(), evaluated_at_epoch=1_000 + COOLDOWN_SECONDS - 1
    )
    exact = evaluate_six_hour_restart_cooldown(
        _state(), evaluated_at_epoch=1_000 + COOLDOWN_SECONDS
    )
    assert before.status == "ACTIVE" and before.remaining_seconds == 1
    assert exact.status == "CLEAR" and exact.cooldown_clear and exact.remaining_seconds == 0
    assert exact == evaluate_six_hour_restart_cooldown(
        _state(), evaluated_at_epoch=1_000 + COOLDOWN_SECONDS
    )
    assert not exact.restart_authorized
    validate_restart_cooldown_decision(exact)


def test_verified_empty_history_is_clear() -> None:
    result = evaluate_six_hour_restart_cooldown(
        _state(has_restart_history=False, last_restart_at_epoch=0), evaluated_at_epoch=2_000
    )
    assert result.status == "CLEAR" and result.next_eligible_at_epoch == 2_000


@pytest.mark.parametrize(
    "overrides",
    [
        {"state_complete": False},
        {"integrity_verified": False},
    ],
)
def test_missing_or_unverified_cooldown_state_denies(overrides) -> None:
    result = evaluate_six_hour_restart_cooldown(_state(**overrides), evaluated_at_epoch=2_000)
    assert result.status == "DENIED" and not result.cooldown_clear


@pytest.mark.parametrize(
    "overrides",
    [
        {"has_restart_history": False, "last_restart_at_epoch": 1},
        {"has_restart_history": True, "last_restart_at_epoch": 0},
        {"last_restart_at_epoch": 2_001},
    ],
)
def test_contradictory_missing_or_future_history_is_tampered(overrides) -> None:
    assert (
        evaluate_six_hour_restart_cooldown(_state(**overrides), evaluated_at_epoch=2_000).status
        == "TAMPERED"
    )


def test_state_decision_malformed_time_and_authority_tampering_fail_closed() -> None:
    state = _state()
    with pytest.raises(SixHourRestartCooldownError, match="STATE_HASH_MISMATCH"):
        evaluate_six_hour_restart_cooldown(
            replace(state, last_restart_at_epoch=999), evaluated_at_epoch=2_000
        )
    with pytest.raises(SixHourRestartCooldownError, match="TIME_INVALID"):
        evaluate_six_hour_restart_cooldown(state, evaluated_at_epoch=-1)
    decision = evaluate_six_hour_restart_cooldown(state, evaluated_at_epoch=2_000)
    with pytest.raises(SixHourRestartCooldownError, match="DECISION_HASH_MISMATCH"):
        validate_restart_cooldown_decision(replace(decision, remaining_seconds=0))
    with pytest.raises(SixHourRestartCooldownError, match="SAFETY_BOUNDARY"):
        validate_restart_cooldown_decision(replace(decision, restart_authorized=True))


def test_cooldown_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "popen", "subprocess", "socket", "shutdown", "systemctl"}
    assert forbidden.isdisjoint(evaluate_six_hour_restart_cooldown.__code__.co_names)
