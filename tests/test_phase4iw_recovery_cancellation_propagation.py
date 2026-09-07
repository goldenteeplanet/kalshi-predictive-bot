from dataclasses import replace

import pytest
from kalshi_predictor.workstation.recovery_cancellation_command import (
    evaluate_recovery_cancellation_command,
    make_recovery_authority_state,
    make_recovery_cancellation_command,
)
from kalshi_predictor.workstation.recovery_cancellation_propagation import (
    RecoveryCancellationPropagationError,
    make_recovery_child_state,
    propagate_recovery_cancellation,
    validate_recovery_cancellation_propagation_decision,
)

PARENT = "a" * 64


def _cancellation():
    state = make_recovery_authority_state(
        incident_id_hash="b" * 64,
        authorization_hash="c" * 64,
        active=True,
        issued_at_epoch_seconds=1,
        expires_at_epoch_seconds=500,
    )
    command = make_recovery_cancellation_command(
        command_id="cancel",
        incident_id_hash="b" * 64,
        target_authorization_hash="c" * 64,
        issued_at_epoch_seconds=100,
        expires_at_epoch_seconds=200,
        operator_identity_hash="d" * 64,
        reason_code="OPERATOR_CANCEL",
        complete=True,
    )
    return evaluate_recovery_cancellation_command(state, command, evaluated_at_epoch_seconds=100)


def _child(n, state="PENDING", **overrides):
    fields = dict(
        child_plan_hash=str(n) * 64,
        parent_plan_hash=PARENT,
        state=state,
        cancelable=True,
        complete=True,
    )
    fields.update(overrides)
    return make_recovery_child_state(**fields)


def test_active_children_cancel_atomically_completed_children_remain_complete() -> None:
    result = propagate_recovery_cancellation(
        _cancellation(), PARENT, [_child(2, "RUNNING"), _child(1), _child(3, "COMPLETE")]
    )
    assert result.status == "PROPAGATED"
    assert result.transitions == (
        ("1" * 64, "PENDING", "CANCELLED"),
        ("2" * 64, "RUNNING", "CANCELLED"),
        ("3" * 64, "COMPLETE", "COMPLETE"),
    )
    assert result.cancellation_executed is False
    assert not any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_recovery_cancellation_propagation_decision(result)


def test_already_terminal_children_are_deterministic_no_action() -> None:
    records = [_child(1, "COMPLETE"), _child(2, "CANCELLED")]
    first = propagate_recovery_cancellation(_cancellation(), PARENT, records)
    second = propagate_recovery_cancellation(_cancellation(), PARENT, list(reversed(records)))
    assert first == second and first.status == "NO_ACTION"


def test_uncancelable_active_incomplete_duplicate_parent_mismatch_and_excess_fail_closed() -> None:
    assert (
        propagate_recovery_cancellation(
            _cancellation(), PARENT, [_child(1, cancelable=False)]
        ).status
        == "DENIED"
    )
    assert (
        propagate_recovery_cancellation(_cancellation(), PARENT, [_child(1, complete=False)]).status
        == "INCOMPLETE"
    )
    assert (
        propagate_recovery_cancellation(_cancellation(), PARENT, [_child(1), _child(1)]).status
        == "TAMPERED"
    )
    assert (
        propagate_recovery_cancellation(
            _cancellation(), PARENT, [_child(1, parent_plan_hash="e" * 64)]
        ).status
        == "TAMPERED"
    )
    with pytest.raises(RecoveryCancellationPropagationError, match="CHILD_BOUND_EXCEEDED"):
        propagate_recovery_cancellation(
            _cancellation(), PARENT, [_child(1), _child(2)], max_children=1
        )


def test_child_decision_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(RecoveryCancellationPropagationError, match="CHILD_HASH_MISMATCH"):
        propagate_recovery_cancellation(
            _cancellation(), PARENT, [replace(_child(1), cancelable=False)]
        )
    result = propagate_recovery_cancellation(_cancellation(), PARENT, [_child(1)])
    with pytest.raises(RecoveryCancellationPropagationError, match="DECISION_HASH_MISMATCH"):
        validate_recovery_cancellation_propagation_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(RecoveryCancellationPropagationError, match="SAFETY_BOUNDARY"):
        validate_recovery_cancellation_propagation_decision(
            replace(result, cancellation_executed=True)
        )
    forbidden = {
        "cancel",
        "terminate",
        "kill",
        "open",
        "run",
        "popen",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(propagate_recovery_cancellation.__code__.co_names)
