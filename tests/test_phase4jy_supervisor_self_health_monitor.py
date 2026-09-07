from dataclasses import replace

import pytest
from kalshi_predictor.workstation.supervisor_self_health_monitor import (
    MAX_HEARTBEAT_AGE_SECONDS,
    MAX_LOOP_DURATION_SECONDS,
    SupervisorSelfHealthMonitorError,
    evaluate_supervisor_self_health,
    make_supervisor_self_health_evidence,
    validate_supervisor_self_health_decision,
)

NOW = 2_000


def _evidence(**overrides):
    fields = dict(
        supervisor_instance_hash="1" * 64,
        heartbeat_hash="2" * 64,
        exclusion_lock_hash="3" * 64,
        evaluated_at_epoch=NOW,
        heartbeat_observed_at_epoch=NOW - 1,
        last_loop_duration_seconds=1,
        consecutive_internal_errors=0,
        lock_owned_by_instance=True,
        heartbeat_integrity_verified=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_supervisor_self_health_evidence(**fields)


def test_fresh_fast_error_free_owned_state_is_healthy_and_non_authorizing() -> None:
    first = evaluate_supervisor_self_health(_evidence())
    assert first == evaluate_supervisor_self_health(_evidence())
    assert first.status == "HEALTHY" and first.supervisor_healthy and not first.alert_required
    assert not any(
        (first.self_restart_authorized, first.host_restart_authorized, first.execution_authorized)
    )
    validate_supervisor_self_health_decision(first)


def test_exact_heartbeat_and_loop_boundaries_are_healthy() -> None:
    result = evaluate_supervisor_self_health(
        _evidence(
            heartbeat_observed_at_epoch=NOW - MAX_HEARTBEAT_AGE_SECONDS,
            last_loop_duration_seconds=MAX_LOOP_DURATION_SECONDS,
        )
    )
    assert result.status == "HEALTHY"


@pytest.mark.parametrize(
    "overrides",
    [
        {"last_loop_duration_seconds": MAX_LOOP_DURATION_SECONDS + 1},
        {"consecutive_internal_errors": 1},
        {"consecutive_internal_errors": 2},
    ],
)
def test_slow_loop_or_bounded_errors_degrade_and_alert(overrides) -> None:
    result = evaluate_supervisor_self_health(_evidence(**overrides))
    assert result.status == "DEGRADED" and result.alert_required
    assert not result.self_restart_authorized


@pytest.mark.parametrize(
    "overrides",
    [
        {"heartbeat_observed_at_epoch": NOW - MAX_HEARTBEAT_AGE_SECONDS - 1},
        {"consecutive_internal_errors": 3},
        {"lock_owned_by_instance": False},
    ],
)
def test_stale_error_limit_or_lost_lock_fails_without_self_restart(overrides) -> None:
    result = evaluate_supervisor_self_health(_evidence(**overrides))
    assert result.status == "FAILED" and result.alert_required
    assert not result.self_restart_authorized and not result.host_restart_authorized


def test_future_incomplete_unverified_and_malformed_evidence_fail_closed() -> None:
    assert (
        evaluate_supervisor_self_health(_evidence(heartbeat_observed_at_epoch=NOW + 1)).status
        == "TAMPERED"
    )
    assert (
        evaluate_supervisor_self_health(_evidence(evidence_complete=False)).status == "INCOMPLETE"
    )
    assert (
        evaluate_supervisor_self_health(_evidence(heartbeat_integrity_verified=False)).status
        == "INCOMPLETE"
    )
    with pytest.raises(SupervisorSelfHealthMonitorError, match="FIELD_INVALID"):
        _evidence(last_loop_duration_seconds=-1)


def test_evidence_decision_and_authority_tampering_fail_closed() -> None:
    evidence = _evidence()
    with pytest.raises(SupervisorSelfHealthMonitorError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_supervisor_self_health(replace(evidence, lock_owned_by_instance=False))
    decision = evaluate_supervisor_self_health(evidence)
    with pytest.raises(SupervisorSelfHealthMonitorError, match="DECISION_HASH_MISMATCH"):
        validate_supervisor_self_health_decision(replace(decision, heartbeat_age_seconds=2))
    with pytest.raises(SupervisorSelfHealthMonitorError, match="SAFETY_BOUNDARY"):
        validate_supervisor_self_health_decision(replace(decision, self_restart_authorized=True))


def test_monitor_has_no_clock_notification_process_or_restart_surface() -> None:
    forbidden = {"time", "open", "write", "run", "Popen", "subprocess", "spawn", "shutdown"}
    assert forbidden.isdisjoint(evaluate_supervisor_self_health.__code__.co_names)
