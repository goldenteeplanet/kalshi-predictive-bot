from dataclasses import replace

import pytest

from kalshi_predictor.workstation.component_recovery_differential_replay import (
    ComponentRecoveryDifferentialReplayError,
    evaluate_component_recovery_differential_replay,
    make_recovery_replay_case,
    validate_component_recovery_replay_decision,
)


def _case(n, **overrides):
    fields = dict(
        scenario_id_hash=str(n) * 64,
        input_hash="a" * 64,
        baseline_status="PLANNED",
        baseline_output_hash="b" * 64,
        baseline_safety_proven=True,
        candidate_status="PLANNED",
        candidate_output_hash="b" * 64,
        candidate_safety_proven=True,
        complete=True,
    )
    fields.update(overrides)
    return make_recovery_replay_case(**fields)


def test_equivalent_safe_cases_match_deterministically_without_authority() -> None:
    first = evaluate_component_recovery_differential_replay([_case(2), _case(1)])
    second = evaluate_component_recovery_differential_replay([_case(1), _case(2)])
    assert first == second and first.status == "MATCH" and first.differential_equivalence_proven
    assert first.matched_count == 2
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_component_recovery_replay_decision(first)


def test_status_or_output_drift_and_safety_regression_fail_closed() -> None:
    assert (
        evaluate_component_recovery_differential_replay(
            [_case(1, candidate_status="DENIED")]
        ).status
        == "DRIFT"
    )
    assert (
        evaluate_component_recovery_differential_replay(
            [_case(1, candidate_output_hash="c" * 64)]
        ).status
        == "DRIFT"
    )
    assert (
        evaluate_component_recovery_differential_replay(
            [_case(1, candidate_safety_proven=False)]
        ).status
        == "SAFETY_REGRESSION"
    )


def test_empty_incomplete_duplicate_excess_and_tampered_cases_fail_closed() -> None:
    assert evaluate_component_recovery_differential_replay([]).status == "INCOMPLETE"
    assert (
        evaluate_component_recovery_differential_replay([_case(1, complete=False)]).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_component_recovery_differential_replay([_case(1), _case(1)]).status == "TAMPERED"
    )
    with pytest.raises(ComponentRecoveryDifferentialReplayError, match="BOUND_EXCEEDED"):
        evaluate_component_recovery_differential_replay([_case(1), _case(2)], max_cases=1)
    with pytest.raises(ComponentRecoveryDifferentialReplayError, match="CASE_HASH_MISMATCH"):
        evaluate_component_recovery_differential_replay(
            [replace(_case(1), candidate_status="DENIED")]
        )


def test_decision_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    result = evaluate_component_recovery_differential_replay([_case(1)])
    with pytest.raises(ComponentRecoveryDifferentialReplayError, match="DECISION_HASH_MISMATCH"):
        validate_component_recovery_replay_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(ComponentRecoveryDifferentialReplayError, match="SAFETY_BOUNDARY"):
        validate_component_recovery_replay_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "systemctl",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_component_recovery_differential_replay.__code__.co_names)
