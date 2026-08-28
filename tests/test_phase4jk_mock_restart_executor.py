from dataclasses import replace

import pytest

from kalshi_predictor.workstation.mock_restart_executor import (
    MockRestartExecutorError,
    execute_mock_restart,
    make_mock_restart_execution_request,
    validate_mock_restart_execution_result,
)


def _request(**overrides):
    fields = dict(
        simulation_id_hash="1" * 64,
        incident_id_hash="2" * 64,
        command_preview_hash="3" * 64,
        executable="shutdown.exe",
        arguments=("/r", "/t", "0"),
        injected_exit_code=0,
        simulation_mode=True,
        dry_run=True,
        complete=True,
    )
    fields.update(overrides)
    return make_mock_restart_execution_request(**fields)


def test_success_is_deterministic_recorded_simulation_only() -> None:
    first = execute_mock_restart(_request())
    assert first == execute_mock_restart(_request())
    assert first.status == "SIMULATED" and first.invocation_recorded
    assert first.restart_effect_simulated and first.mock_only
    assert not any(
        (first.restart_authorized, first.process_spawn_authorized, first.execution_authorized)
    )
    validate_mock_restart_execution_result(first)


def test_injected_nonzero_exit_is_simulated_failure() -> None:
    result = execute_mock_restart(_request(injected_exit_code=5))
    assert result.status == "SIMULATED_FAILURE" and result.invocation_recorded
    assert not result.restart_effect_simulated and result.simulated_exit_code == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"simulation_mode": False},
        {"dry_run": False},
        {"executable": "Restart-Computer"},
        {"arguments": ("/r", "/t", "1")},
    ],
)
def test_real_mode_or_command_drift_is_denied(overrides) -> None:
    result = execute_mock_restart(_request(**overrides))
    assert result.status == "DENIED" and not result.invocation_recorded


def test_force_flag_incomplete_and_malformed_requests_fail_closed() -> None:
    assert execute_mock_restart(_request(arguments=("/r", "/f", "/t", "0"))).status == "TAMPERED"
    assert execute_mock_restart(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(MockRestartExecutorError, match="FIELD_INVALID"):
        _request(injected_exit_code=256)


def test_request_result_and_authority_tampering_fail_closed() -> None:
    request = _request()
    with pytest.raises(MockRestartExecutorError, match="REQUEST_HASH_MISMATCH"):
        execute_mock_restart(replace(request, dry_run=False))
    result = execute_mock_restart(request)
    with pytest.raises(MockRestartExecutorError, match="RESULT_HASH_MISMATCH"):
        validate_mock_restart_execution_result(replace(result, invocation_hash="f" * 64))
    with pytest.raises(MockRestartExecutorError, match="SAFETY_BOUNDARY"):
        validate_mock_restart_execution_result(replace(result, process_spawn_authorized=True))


def test_executor_has_no_process_shell_or_operational_surface() -> None:
    forbidden = {"open", "run", "Popen", "subprocess", "system", "spawn", "socket", "os"}
    assert forbidden.isdisjoint(execute_mock_restart.__code__.co_names)
