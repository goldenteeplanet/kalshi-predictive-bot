from dataclasses import replace

import pytest
from kalshi_predictor.workstation.seven_day_restart_budget import (
    WINDOW_SECONDS,
    SevenDayRestartBudgetError,
    evaluate_seven_day_restart_budget,
    make_restart_budget_state,
    validate_restart_budget_decision,
)

NOW = 2_000_000


def _state(**overrides):
    fields = dict(
        state_id_hash="1" * 64,
        history_file_hash="2" * 64,
        restart_epochs=(),
        state_complete=True,
        integrity_verified=True,
    )
    fields.update(overrides)
    return make_restart_budget_state(**fields)


def test_zero_or_one_restart_leaves_budget_without_authority() -> None:
    empty = evaluate_seven_day_restart_budget(_state(), evaluated_at_epoch=NOW)
    one = evaluate_seven_day_restart_budget(
        _state(restart_epochs=(NOW - 1,)), evaluated_at_epoch=NOW
    )
    assert empty.status == one.status == "AVAILABLE"
    assert empty.remaining_restarts == 2 and one.remaining_restarts == 1
    assert not one.restart_authorized
    validate_restart_budget_decision(one)


def test_two_restarts_exhaust_budget_and_order_is_canonical() -> None:
    epochs = (NOW - 20, NOW - 10)
    first = evaluate_seven_day_restart_budget(_state(restart_epochs=epochs), evaluated_at_epoch=NOW)
    second = evaluate_seven_day_restart_budget(
        _state(restart_epochs=tuple(reversed(epochs))), evaluated_at_epoch=NOW
    )
    assert first == second
    assert first.status == second.status == "EXHAUSTED"
    assert first.in_window_epochs == second.in_window_epochs == epochs
    assert first.remaining_restarts == 0


def test_exact_seven_day_boundary_is_aged_out() -> None:
    result = evaluate_seven_day_restart_budget(
        _state(restart_epochs=(NOW - WINDOW_SECONDS, NOW - 1)), evaluated_at_epoch=NOW
    )
    assert result.status == "AVAILABLE" and result.restarts_in_window == 1


@pytest.mark.parametrize("overrides", [{"state_complete": False}, {"integrity_verified": False}])
def test_incomplete_or_unverified_state_denies(overrides) -> None:
    assert (
        evaluate_seven_day_restart_budget(_state(**overrides), evaluated_at_epoch=NOW).status
        == "DENIED"
    )


def test_duplicate_future_excess_and_malformed_history_fail_closed() -> None:
    assert (
        evaluate_seven_day_restart_budget(
            _state(restart_epochs=(1, 1)), evaluated_at_epoch=NOW
        ).status
        == "TAMPERED"
    )
    assert (
        evaluate_seven_day_restart_budget(
            _state(restart_epochs=(NOW + 1,)), evaluated_at_epoch=NOW
        ).status
        == "TAMPERED"
    )
    with pytest.raises(SevenDayRestartBudgetError, match="HISTORY_BOUND_EXCEEDED"):
        evaluate_seven_day_restart_budget(
            _state(restart_epochs=(1, 2)), evaluated_at_epoch=NOW, max_history_records=1
        )
    with pytest.raises(SevenDayRestartBudgetError, match="FIELD_INVALID"):
        _state(restart_epochs=(-1,))


def test_state_decision_time_and_authority_tampering_fail_closed() -> None:
    state = _state(restart_epochs=(1,))
    with pytest.raises(SevenDayRestartBudgetError, match="STATE_HASH_MISMATCH"):
        evaluate_seven_day_restart_budget(
            replace(state, restart_epochs=(2,)), evaluated_at_epoch=NOW
        )
    with pytest.raises(SevenDayRestartBudgetError, match="TIME_INVALID"):
        evaluate_seven_day_restart_budget(state, evaluated_at_epoch=-1)
    decision = evaluate_seven_day_restart_budget(state, evaluated_at_epoch=NOW)
    with pytest.raises(SevenDayRestartBudgetError, match="DECISION_HASH_MISMATCH"):
        validate_restart_budget_decision(replace(decision, remaining_restarts=0))
    with pytest.raises(SevenDayRestartBudgetError, match="SAFETY_BOUNDARY"):
        validate_restart_budget_decision(replace(decision, restart_authorized=True))


def test_budget_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "popen", "subprocess", "socket", "shutdown", "systemctl"}
    assert forbidden.isdisjoint(evaluate_seven_day_restart_budget.__code__.co_names)
