from dataclasses import replace

import pytest
from kalshi_predictor.workstation.restart_guard_stress_test import (
    COOLDOWN_SECONDS,
    RestartGuardStressTestError,
    evaluate_restart_guard_stress_test,
    make_restart_guard_trial,
    validate_restart_guard_stress_report,
)


def _trial(elapsed, budget, attempts, observed=None):
    expected = elapsed >= COOLDOWN_SECONDS and budget < 2 and attempts == 0
    return make_restart_guard_trial(
        cooldown_elapsed_seconds=elapsed,
        restarts_in_seven_days=budget,
        incident_restart_attempts=attempts,
        observed_eligible=expected if observed is None else observed,
    )


def _trials():
    return (
        _trial(21_599, 0, 0),
        _trial(21_600, 0, 0),
        _trial(21_601, 1, 0),
        _trial(21_600, 2, 0),
        _trial(99_999, 3, 0),
        _trial(21_600, 0, 1),
        _trial(0, 2, 1),
        _trial(99_999, 1, 2),
    )


def test_boundary_stress_matrix_passes_deterministically_without_authority() -> None:
    first = evaluate_restart_guard_stress_test(_trials())
    assert first == evaluate_restart_guard_stress_test(_trials())
    assert first.status == "PASSED" and first.trial_count == 8 and first.mismatch_count == 0
    assert (
        first.cooldown_boundaries_covered
        and first.budget_boundaries_covered
        and first.loop_breaker_boundaries_covered
    )
    assert not first.restart_authorized
    validate_restart_guard_stress_report(first)


@pytest.mark.parametrize("index", range(8))
def test_every_inverted_guard_result_is_detected(index) -> None:
    trials = list(_trials())
    trials[index] = replace(trials[index], observed_eligible=not trials[index].observed_eligible)
    result = evaluate_restart_guard_stress_test(tuple(trials))
    assert result.status == "FAILED" and result.mismatch_count == 1


def test_missing_trials_or_each_boundary_is_incomplete() -> None:
    assert evaluate_restart_guard_stress_test(_trials()[:-1]).status == "INCOMPLETE"
    no_cooldown_edge = tuple(
        _trial(30_000, t.restarts_in_seven_days, t.incident_restart_attempts) for t in _trials()
    )
    assert evaluate_restart_guard_stress_test(no_cooldown_edge).status == "INCOMPLETE"
    no_budget_edge = tuple(
        _trial(t.cooldown_elapsed_seconds, 0, t.incident_restart_attempts) for t in _trials()
    )
    assert evaluate_restart_guard_stress_test(no_budget_edge).status == "INCOMPLETE"


def test_trial_report_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(RestartGuardStressTestError, match="TRIAL_FIELD_INVALID"):
        _trial(-1, 0, 0)
    report = evaluate_restart_guard_stress_test(_trials())
    with pytest.raises(RestartGuardStressTestError, match="REPORT_HASH_MISMATCH"):
        validate_restart_guard_stress_report(replace(report, mismatch_count=1))
    with pytest.raises(RestartGuardStressTestError, match="SAFETY_BOUNDARY"):
        validate_restart_guard_stress_report(replace(report, restart_authorized=True))
    forbidden = {"open", "write", "run", "Popen", "subprocess", "restart", "shutdown", "execute"}
    assert forbidden.isdisjoint(evaluate_restart_guard_stress_test.__code__.co_names)
