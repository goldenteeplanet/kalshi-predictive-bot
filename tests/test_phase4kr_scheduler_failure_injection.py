from dataclasses import replace

import pytest
from kalshi_predictor.workstation.scheduler_failure_injection import (
    SchedulerFailureInjectionError,
    evaluate_scheduler_failure_injection,
    make_scheduler_failure_evidence,
    validate_scheduler_failure_result,
)


def _evidence(**overrides):
    fields = dict(
        incident_hash="1" * 64,
        scheduler_identity_hash="2" * 64,
        failure_injected=True,
        failure_detected=True,
        recovery_attempt_count=1,
        recovery_verification_performed=True,
        recovery_verified=False,
        escalation_generated=True,
        uncontrolled_restart_progressed=False,
        writer_count_after=0,
        complete=True,
    )
    fields.update(overrides)
    return make_scheduler_failure_evidence(**fields)


def test_failed_bounded_recovery_escalates_without_uncontrolled_restart() -> None:
    first = evaluate_scheduler_failure_injection(_evidence())
    assert first == evaluate_scheduler_failure_injection(_evidence())
    assert first.status == "PASSED" and first.bounded_recovery_proven and first.escalation_proven
    assert first.writer_safety_proven and not first.restart_authorized
    validate_scheduler_failure_result(first)


def test_successful_one_attempt_recovery_restores_one_writer() -> None:
    result = evaluate_scheduler_failure_injection(
        _evidence(recovery_verified=True, escalation_generated=False, writer_count_after=1)
    )
    assert result.status == "PASSED" and result.writer_safety_proven


@pytest.mark.parametrize(
    "overrides",
    [
        {"failure_detected": False},
        {"recovery_attempt_count": 0},
        {"recovery_attempt_count": 2},
        {"recovery_verification_performed": False},
        {"escalation_generated": False},
        {"uncontrolled_restart_progressed": True},
        {"writer_count_after": 1},
    ],
)
def test_unbounded_unverified_unalerted_or_unsafe_behavior_fails(overrides) -> None:
    assert evaluate_scheduler_failure_injection(_evidence(**overrides)).status == "FAILED"


def test_incomplete_absent_and_contradictory_injection_fail_closed() -> None:
    assert evaluate_scheduler_failure_injection(_evidence(complete=False)).status == "INCOMPLETE"
    assert (
        evaluate_scheduler_failure_injection(_evidence(failure_injected=False)).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_scheduler_failure_injection(
            _evidence(recovery_verification_performed=False, recovery_verified=True)
        ).status
        == "TAMPERED"
    )


def test_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(SchedulerFailureInjectionError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_scheduler_failure_injection(replace(_evidence(), recovery_attempt_count=2))
    result = evaluate_scheduler_failure_injection(_evidence())
    with pytest.raises(SchedulerFailureInjectionError, match="RESULT_HASH_MISMATCH"):
        validate_scheduler_failure_result(replace(result, escalation_proven=False))
    with pytest.raises(SchedulerFailureInjectionError, match="SAFETY_BOUNDARY"):
        validate_scheduler_failure_result(replace(result, service_control_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "start",
        "stop",
    }
    assert forbidden.isdisjoint(evaluate_scheduler_failure_injection.__code__.co_names)
