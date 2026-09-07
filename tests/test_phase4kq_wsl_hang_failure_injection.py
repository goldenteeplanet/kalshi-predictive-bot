from dataclasses import replace

import pytest

from kalshi_predictor.workstation.wsl_hang_failure_injection import (
    WslHangFailureInjectionError,
    evaluate_wsl_hang_failure_injection,
    make_wsl_hang_injection_evidence,
    validate_wsl_hang_injection_result,
)


def _evidence(**overrides):
    fields = dict(
        incident_hash="1" * 64,
        probe_identity_hash="2" * 64,
        timeout_milliseconds=5_000,
        observed_duration_milliseconds=5_000,
        hang_injected=True,
        timeout_observed=True,
        probe_terminated=True,
        escalation_generated=True,
        recovery_progressed=False,
        restart_progressed=False,
        complete=True,
    )
    fields.update(overrides)
    return make_wsl_hang_injection_evidence(**fields)


def test_wsl_hang_times_out_terminates_escalates_and_stops() -> None:
    first = evaluate_wsl_hang_failure_injection(_evidence())
    assert first == evaluate_wsl_hang_failure_injection(_evidence())
    assert (
        first.status == "PASSED" and first.bounded_termination_proven and first.fail_closed_proven
    )
    assert not any(
        (first.recovery_authorized, first.restart_authorized, first.process_control_authorized)
    )
    validate_wsl_hang_injection_result(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_observed": False},
        {"probe_terminated": False},
        {"escalation_generated": False},
        {"recovery_progressed": True},
        {"restart_progressed": True},
    ],
)
def test_missing_containment_or_unsafe_progression_fails(overrides) -> None:
    assert evaluate_wsl_hang_failure_injection(_evidence(**overrides)).status == "FAILED"


def test_no_injection_incomplete_and_early_timeout_fail_closed() -> None:
    assert (
        evaluate_wsl_hang_failure_injection(_evidence(hang_injected=False)).status == "INCOMPLETE"
    )
    assert evaluate_wsl_hang_failure_injection(_evidence(complete=False)).status == "INCOMPLETE"
    assert (
        evaluate_wsl_hang_failure_injection(_evidence(observed_duration_milliseconds=4_999)).status
        == "TAMPERED"
    )


def test_evidence_result_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(WslHangFailureInjectionError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_wsl_hang_failure_injection(replace(_evidence(), probe_terminated=False))
    result = evaluate_wsl_hang_failure_injection(_evidence())
    with pytest.raises(WslHangFailureInjectionError, match="RESULT_HASH_MISMATCH"):
        validate_wsl_hang_injection_result(replace(result, hang_injected=False))
    with pytest.raises(WslHangFailureInjectionError, match="SAFETY_BOUNDARY"):
        validate_wsl_hang_injection_result(replace(result, restart_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "spawn",
        "kill",
        "terminate",
        "wsl",
        "restart",
        "shutdown",
    }
    assert forbidden.isdisjoint(evaluate_wsl_hang_failure_injection.__code__.co_names)
