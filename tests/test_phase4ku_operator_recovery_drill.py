from dataclasses import replace

import pytest
from kalshi_predictor.workstation.operator_recovery_drill import (
    REQUIRED_STEPS,
    OperatorRecoveryDrillError,
    evaluate_operator_recovery_drill,
    make_operator_drill_step,
    validate_operator_recovery_drill_result,
)


def _steps(**override):
    result = []
    for index, name in enumerate(REQUIRED_STEPS):
        fields = dict(
            name=name,
            completed_at_epoch_seconds=1_000 + index,
            evidence_hash=f"{index + 1:064x}",
            completed=True,
            simulated_only=True,
        )
        if name == override.get("name"):
            fields.update({key: value for key, value in override.items() if key != "name"})
        result.append(make_operator_drill_step(**fields))
    return tuple(result)


def test_complete_ordered_paper_only_operator_drill_passes() -> None:
    first = evaluate_operator_recovery_drill(_steps())
    assert first == evaluate_operator_recovery_drill(_steps())
    assert first.status == "PASSED" and first.completed_steps == len(REQUIRED_STEPS)
    assert (
        first.ordered_execution_proven and first.paper_only_proven and first.operator_handoff_proven
    )
    assert not any((first.restart_authorized, first.trading_authorized, first.execution_authorized))
    validate_operator_recovery_drill_result(first)


def test_missing_reordered_duplicate_or_time_reversed_steps_fail_closed() -> None:
    steps = _steps()
    assert evaluate_operator_recovery_drill(steps[:-1]).status == "INCOMPLETE"
    assert evaluate_operator_recovery_drill(tuple(reversed(steps))).status == "INCOMPLETE"
    assert evaluate_operator_recovery_drill(steps[:-1] + (steps[0],)).status == "TAMPERED"
    reversed_time = list(steps)
    reversed_time[5] = replace(reversed_time[5], completed_at_epoch_seconds=0)
    assert evaluate_operator_recovery_drill(tuple(reversed_time)).status == "TAMPERED"


@pytest.mark.parametrize(("field", "value"), [("completed", False), ("simulated_only", False)])
def test_incomplete_or_live_step_fails_drill(field, value) -> None:
    result = evaluate_operator_recovery_drill(
        _steps(name="REVIEW_MOCKED_RESTART_RESULT", **{field: value})
    )
    assert result.status == "FAILED" and not result.paper_only_proven


def test_step_result_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(OperatorRecoveryDrillError, match="STEP_FIELD_INVALID"):
        make_operator_drill_step(
            name="UNKNOWN", completed_at_epoch_seconds=1, evidence_hash="1" * 64
        )
    result = evaluate_operator_recovery_drill(_steps())
    with pytest.raises(OperatorRecoveryDrillError, match="RESULT_HASH_MISMATCH"):
        validate_operator_recovery_drill_result(replace(result, completed_steps=0))
    with pytest.raises(OperatorRecoveryDrillError, match="SAFETY_BOUNDARY"):
        validate_operator_recovery_drill_result(replace(result, trading_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
        "order",
    }
    assert forbidden.isdisjoint(evaluate_operator_recovery_drill.__code__.co_names)
