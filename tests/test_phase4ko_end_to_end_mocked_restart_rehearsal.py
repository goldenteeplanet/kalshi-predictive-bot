from dataclasses import replace

import pytest

from kalshi_predictor.workstation.end_to_end_mocked_restart_rehearsal import (
    EndToEndMockedRestartRehearsalError,
    evaluate_end_to_end_mocked_restart_rehearsal,
    validate_mocked_restart_rehearsal_result,
)
from kalshi_predictor.workstation.end_to_end_recovery_dry_run import (
    REQUIRED_STEPS,
    evaluate_end_to_end_recovery_dry_run,
    make_dry_run_step,
)
from kalshi_predictor.workstation.mock_restart_executor import (
    execute_mock_restart,
    make_mock_restart_execution_request,
)


def _dry_run():
    return evaluate_end_to_end_recovery_dry_run(
        tuple(
            make_dry_run_step(name=name, status="PASS", evidence_hash=f"{index + 1:064x}")
            for index, name in enumerate(REQUIRED_STEPS)
        )
    )


def _mock(exit_code=0):
    request = make_mock_restart_execution_request(
        simulation_id_hash="a" * 64,
        incident_id_hash="b" * 64,
        command_preview_hash="c" * 64,
        executable="shutdown.exe",
        arguments=("/r", "/t", "0"),
        injected_exit_code=exit_code,
        simulation_mode=True,
        dry_run=True,
        complete=True,
    )
    return execute_mock_restart(request)


def _evaluate(mock=None, **overrides):
    fields = dict(
        pre_boot_identity_hash="1" * 64,
        simulated_post_boot_identity_hash="2" * 64,
        post_boot_workstream_gate_hash="3" * 64,
        post_boot_workstream_passed=True,
    )
    fields.update(overrides)
    return evaluate_end_to_end_mocked_restart_rehearsal(_dry_run(), mock or _mock(), **fields)


def test_mocked_restart_and_post_boot_chain_pass_without_authority() -> None:
    first = _evaluate()
    assert first == _evaluate() and first.status == "PASSED"
    assert first.mock_only and first.boot_transition_simulated and first.post_boot_chain_verified
    assert not any(
        (first.restart_authorized, first.process_spawn_authorized, first.execution_authorized)
    )
    validate_mocked_restart_rehearsal_result(first)


def test_injected_restart_failure_and_post_boot_failure_fail_rehearsal() -> None:
    assert _evaluate(_mock(5)).status == "FAILED"
    assert _evaluate(post_boot_workstream_passed=False).status == "FAILED"


def test_unchanged_simulated_boot_identity_is_tampered() -> None:
    result = _evaluate(simulated_post_boot_identity_hash="1" * 64)
    assert result.status == "TAMPERED" and not result.boot_transition_simulated


def test_tampered_inputs_and_fields_fail_closed() -> None:
    with pytest.raises(EndToEndMockedRestartRehearsalError, match="INPUT_INVALID"):
        _evaluate(replace(_mock(), mock_only=False))
    with pytest.raises(EndToEndMockedRestartRehearsalError, match="FIELD_INVALID"):
        _evaluate(post_boot_workstream_gate_hash="bad")


def test_result_tampering_and_operational_surfaces_fail_closed() -> None:
    result = _evaluate()
    with pytest.raises(EndToEndMockedRestartRehearsalError, match="RESULT_HASH_MISMATCH"):
        validate_mocked_restart_rehearsal_result(replace(result, pre_boot_identity_hash="4" * 64))
    with pytest.raises(EndToEndMockedRestartRehearsalError, match="SAFETY_BOUNDARY"):
        validate_mocked_restart_rehearsal_result(replace(result, process_spawn_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "spawn",
        "system",
        "shutdown",
        "restart",
        "execute",
    }
    assert forbidden.isdisjoint(evaluate_end_to_end_mocked_restart_rehearsal.__code__.co_names)
