from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_cancellation_command import (
    RecoveryCancellationCommandError,
    evaluate_recovery_cancellation_command,
    make_recovery_authority_state,
    make_recovery_cancellation_command,
    validate_recovery_cancellation_result,
)

INCIDENT = "1" * 64
AUTHORIZATION = "2" * 64
OPERATOR = "3" * 64


def _state(*, active: bool = True):
    return make_recovery_authority_state(
        incident_id_hash=INCIDENT,
        authorization_hash=AUTHORIZATION,
        active=active,
        issued_at_epoch_seconds=900,
        expires_at_epoch_seconds=2_000,
    )


def _command(**overrides):
    values = {
        "command_id": "cancel-001",
        "incident_id_hash": INCIDENT,
        "target_authorization_hash": AUTHORIZATION,
        "issued_at_epoch_seconds": 1_000,
        "expires_at_epoch_seconds": 1_300,
        "operator_identity_hash": OPERATOR,
        "reason_code": "OPERATOR_REVOKED",
        "complete": True,
    }
    values.update(overrides)
    return make_recovery_cancellation_command(**values)


def test_active_authority_is_cancelled_deterministically_without_granting_authority() -> None:
    first = evaluate_recovery_cancellation_command(
        _state(), _command(), evaluated_at_epoch_seconds=1_100
    )
    second = evaluate_recovery_cancellation_command(
        _state(), _command(), evaluated_at_epoch_seconds=1_100
    )

    assert first == second
    assert first.status == "CANCELLED"
    assert first.authority_active_before is True
    assert first.authority_active_after is False
    assert first.cancellation_validated is True
    assert first.cancellation_applied is True
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_recovery_cancellation_result(first)


def test_inactive_authority_is_an_idempotent_no_op() -> None:
    result = evaluate_recovery_cancellation_command(
        _state(active=False), _command(), evaluated_at_epoch_seconds=1_100
    )

    assert result.status == "ALREADY_CANCELLED"
    assert result.authority_active_before is False
    assert result.authority_active_after is False
    assert result.cancellation_validated is True
    assert result.cancellation_applied is False
    validate_recovery_cancellation_result(result)


def test_time_boundaries_are_fail_closed() -> None:
    exact = evaluate_recovery_cancellation_command(
        _state(), _command(), evaluated_at_epoch_seconds=1_300
    )
    expired = evaluate_recovery_cancellation_command(
        _state(), _command(), evaluated_at_epoch_seconds=1_301
    )
    excessive = evaluate_recovery_cancellation_command(
        _state(), _command(expires_at_epoch_seconds=1_301), evaluated_at_epoch_seconds=1_100
    )

    assert exact.status == "CANCELLED"
    assert expired.status == "STALE"
    assert excessive.status == "DENIED"
    assert excessive.reasons == ("RECOVERY_CANCELLATION_COMMAND_TTL_INVALID",)


def test_incomplete_binding_mismatches_and_future_commands_are_rejected() -> None:
    incomplete = evaluate_recovery_cancellation_command(
        _state(), _command(complete=False), evaluated_at_epoch_seconds=1_100
    )
    mismatched = evaluate_recovery_cancellation_command(
        _state(),
        _command(incident_id_hash="4" * 64, target_authorization_hash="5" * 64),
        evaluated_at_epoch_seconds=1_100,
    )
    future = evaluate_recovery_cancellation_command(
        _state(), _command(issued_at_epoch_seconds=1_101), evaluated_at_epoch_seconds=1_100
    )

    assert incomplete.status == "INCOMPLETE"
    assert mismatched.status == "TAMPERED"
    assert mismatched.reasons == (
        "CANCELLATION_INCIDENT_BINDING_MISMATCH",
        "CANCELLATION_AUTHORIZATION_BINDING_MISMATCH",
    )
    assert future.status == "DENIED"


def test_malformed_fields_and_bounds_fail_before_evaluation() -> None:
    with pytest.raises(RecoveryCancellationCommandError, match="AUTHORITY_STATE_FIELD_INVALID"):
        make_recovery_authority_state(
            incident_id_hash="not-a-hash",
            authorization_hash=AUTHORIZATION,
            active=True,
            issued_at_epoch_seconds=0,
            expires_at_epoch_seconds=1,
        )
    with pytest.raises(RecoveryCancellationCommandError, match="COMMAND_FIELD_INVALID"):
        _command(reason_code=" ")
    with pytest.raises(RecoveryCancellationCommandError, match="COMMAND_BOUND_INVALID"):
        evaluate_recovery_cancellation_command(
            _state(), _command(), evaluated_at_epoch_seconds=1_100, max_command_ttl_seconds=0
        )


def test_state_command_result_and_safety_tampering_fail_closed() -> None:
    state = _state()
    command = _command()
    result = evaluate_recovery_cancellation_command(
        state, command, evaluated_at_epoch_seconds=1_100
    )

    with pytest.raises(RecoveryCancellationCommandError, match="AUTHORITY_STATE_HASH_MISMATCH"):
        evaluate_recovery_cancellation_command(
            replace(state, active=False), command, evaluated_at_epoch_seconds=1_100
        )
    with pytest.raises(RecoveryCancellationCommandError, match="COMMAND_HASH_MISMATCH"):
        evaluate_recovery_cancellation_command(
            state, replace(command, complete=False), evaluated_at_epoch_seconds=1_100
        )
    with pytest.raises(RecoveryCancellationCommandError, match="RESULT_HASH_MISMATCH"):
        validate_recovery_cancellation_result(replace(result, reasons=("FORGED",)))
    with pytest.raises(RecoveryCancellationCommandError, match="RESULT_SAFETY_BOUNDARY_INVALID"):
        validate_recovery_cancellation_result(replace(result, host_restart_authorized=True))


def test_evaluator_has_no_io_service_restart_or_execution_surface() -> None:
    forbidden = {
        "open",
        "subprocess",
        "system",
        "popen",
        "requests",
        "socket",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }

    assert forbidden.isdisjoint(evaluate_recovery_cancellation_command.__code__.co_names)
