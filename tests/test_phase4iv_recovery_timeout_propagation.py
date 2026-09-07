from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_timeout_propagation import (
    RecoveryTimeoutPropagationError,
    make_recovery_timeout_step,
    propagate_recovery_timeout,
    validate_recovery_timeout_decision,
)


def _step(code, seconds, complete=True):
    return make_recovery_timeout_step(step_code=code, requested_seconds=seconds, complete=complete)


def test_exact_parent_budget_propagates_monotonic_child_deadlines_without_authority() -> None:
    result = propagate_recovery_timeout(
        "a" * 64,
        [_step("VERIFY", 10), _step("PLAN", 20)],
        issued_at_epoch_seconds=100,
        parent_deadline_epoch_seconds=130,
        evaluated_at_epoch_seconds=100,
    )
    assert result.status == "PROPAGATED" and result.step_deadlines == (
        ("VERIFY", 110),
        ("PLAN", 130),
    )
    assert result.timeout_propagation_proven
    assert not any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_recovery_timeout_decision(result)


def test_one_second_excess_expiry_and_future_issue_fail_closed() -> None:
    assert (
        propagate_recovery_timeout(
            "a" * 64,
            [_step("A", 31)],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
        ).status
        == "DENIED"
    )
    assert (
        propagate_recovery_timeout(
            "a" * 64,
            [_step("A", 1)],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=130,
        ).status
        == "EXPIRED"
    )
    assert (
        propagate_recovery_timeout(
            "a" * 64,
            [_step("A", 1)],
            issued_at_epoch_seconds=101,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
        ).status
        == "TAMPERED"
    )


def test_empty_incomplete_duplicate_excess_count_and_tampered_steps_fail_closed() -> None:
    assert (
        propagate_recovery_timeout(
            "a" * 64,
            [],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
        ).status
        == "INCOMPLETE"
    )
    assert (
        propagate_recovery_timeout(
            "a" * 64,
            [_step("A", 1, False)],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
        ).status
        == "INCOMPLETE"
    )
    assert (
        propagate_recovery_timeout(
            "a" * 64,
            [_step("A", 1), _step("A", 1)],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
        ).status
        == "TAMPERED"
    )
    with pytest.raises(RecoveryTimeoutPropagationError, match="STEP_BOUND_EXCEEDED"):
        propagate_recovery_timeout(
            "a" * 64,
            [_step("A", 1), _step("B", 1)],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
            max_steps=1,
        )
    with pytest.raises(RecoveryTimeoutPropagationError, match="STEP_HASH_MISMATCH"):
        propagate_recovery_timeout(
            "a" * 64,
            [replace(_step("A", 1), requested_seconds=2)],
            issued_at_epoch_seconds=100,
            parent_deadline_epoch_seconds=130,
            evaluated_at_epoch_seconds=100,
        )


def test_decision_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    result = propagate_recovery_timeout(
        "a" * 64,
        [_step("A", 1)],
        issued_at_epoch_seconds=100,
        parent_deadline_epoch_seconds=130,
        evaluated_at_epoch_seconds=100,
    )
    with pytest.raises(RecoveryTimeoutPropagationError, match="DECISION_HASH_MISMATCH"):
        validate_recovery_timeout_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(RecoveryTimeoutPropagationError, match="CHILD_DEADLINE_INVALID"):
        validate_recovery_timeout_decision(replace(result, step_deadlines=(("A", 131),)))
    with pytest.raises(RecoveryTimeoutPropagationError, match="SAFETY_BOUNDARY"):
        validate_recovery_timeout_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "sleep",
        "timer",
        "open",
        "run",
        "popen",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(propagate_recovery_timeout.__code__.co_names)
