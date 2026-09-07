from dataclasses import replace

import pytest
from kalshi_predictor.workstation.non_forced_restart_command_adapter import (
    COMMAND_ARGUMENTS,
    EXECUTABLE,
    NonForcedRestartCommandAdapterError,
    build_non_forced_restart_command_preview,
    make_restart_command_request,
    validate_restart_command_preview,
)


def _request(**overrides):
    fields = dict(
        incident_id_hash="1" * 64,
        restart_intent_hash="2" * 64,
        warning_decision_hash="3" * 64,
        cooldown_decision_hash="4" * 64,
        budget_decision_hash="5" * 64,
        loop_breaker_decision_hash="6" * 64,
        executable=EXECUTABLE,
        arguments=COMMAND_ARGUMENTS,
        dry_run=True,
        complete=True,
    )
    fields.update(overrides)
    return make_restart_command_request(**fields)


def test_exact_non_forced_command_is_deterministic_preview_only() -> None:
    first = build_non_forced_restart_command_preview(_request())
    assert first == build_non_forced_restart_command_preview(_request())
    assert first.status == "PREVIEW_READY" and first.non_forced_proven
    assert first.executable == "shutdown.exe" and first.arguments == ("/r", "/t", "0")
    assert "/f" not in first.arguments
    assert not any(
        (first.restart_authorized, first.process_spawn_authorized, first.execution_authorized)
    )
    validate_restart_command_preview(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"dry_run": False},
        {"executable": "Restart-Computer"},
        {"arguments": ("/r", "/t", "1")},
    ],
)
def test_non_dry_run_alternate_executor_or_arguments_are_denied(overrides) -> None:
    result = build_non_forced_restart_command_preview(_request(**overrides))
    assert result.status == "DENIED" and not result.command_preview_ready


@pytest.mark.parametrize("flag", ["/f", "-Force", "/force"])
def test_force_flags_are_explicitly_tampered(flag) -> None:
    result = build_non_forced_restart_command_preview(_request(arguments=("/r", "/t", "0", flag)))
    assert result.status == "TAMPERED" and "FORCE_FLAG_REFUSED" in result.reasons[0]


def test_incomplete_malformed_and_request_tampering_fail_closed() -> None:
    assert build_non_forced_restart_command_preview(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(NonForcedRestartCommandAdapterError, match="FIELD_INVALID"):
        _request(executable="bad path.exe")
    with pytest.raises(NonForcedRestartCommandAdapterError, match="REQUEST_HASH_MISMATCH"):
        build_non_forced_restart_command_preview(replace(_request(), dry_run=False))


def test_preview_and_authority_tampering_fail_closed() -> None:
    preview = build_non_forced_restart_command_preview(_request())
    with pytest.raises(NonForcedRestartCommandAdapterError, match="PREVIEW_HASH_MISMATCH"):
        validate_restart_command_preview(replace(preview, command_hash="f" * 64))
    with pytest.raises(NonForcedRestartCommandAdapterError, match="SAFETY_BOUNDARY"):
        validate_restart_command_preview(replace(preview, process_spawn_authorized=True))


def test_adapter_has_no_process_or_operational_surface() -> None:
    forbidden = {"open", "run", "Popen", "subprocess", "system", "spawn", "socket", "systemctl"}
    assert forbidden.isdisjoint(build_non_forced_restart_command_preview.__code__.co_names)
