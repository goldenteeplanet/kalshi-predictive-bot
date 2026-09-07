from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    AuthoritativeSchedulerHealthError,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
    validate_scheduler_health_evidence,
)


def test_healthy_scheduler_evidence_is_deterministic_exclusive_and_read_only() -> None:
    observation = _observation()
    first = probe_authoritative_scheduler_health(observation)
    second = probe_authoritative_scheduler_health(observation)
    validate_scheduler_health_evidence(first)
    assert first.status == "HEALTHY"
    assert first.writer_exclusive is True
    assert first.evidence_hash == second.evidence_hash
    assert first.service_control_authorized is False


def test_unhealthy_states_and_writer_violation_are_explicit() -> None:
    evidence = probe_authoritative_scheduler_health(
        _observation(
            load="not-found", active="inactive", sub="dead", pid=0, writers=2, duration=5_001
        )
    )
    assert evidence.status == "UNHEALTHY"
    assert evidence.writer_exclusive is False
    assert evidence.reasons == tuple(sorted(evidence.reasons))
    assert "WRITER_EXCLUSIVITY_VIOLATION" in evidence.reasons
    assert evidence.host_restart_authorized is False


def test_exact_identity_duration_and_freshness_boundaries() -> None:
    exact = probe_authoritative_scheduler_health(_observation(duration=5_000, age=120))
    assert exact.status == "HEALTHY"
    assert probe_authoritative_scheduler_health(_observation(duration=5_001)).status == "UNHEALTHY"
    assert probe_authoritative_scheduler_health(_observation(age=121)).status == "STALE"
    mismatch = probe_authoritative_scheduler_health(_observation(unit="other.service"))
    assert mismatch.status == "IDENTITY_MISMATCH"


def test_incomplete_and_missing_writer_evidence_fail_closed() -> None:
    incomplete = probe_authoritative_scheduler_health(
        _observation(complete=False, pid=None, writers=None)
    )
    assert incomplete.status == "INCOMPLETE"
    assert incomplete.reasons == ("SCHEDULER_PROBE_INCOMPLETE",)
    missing = probe_authoritative_scheduler_health(_observation(writers=None))
    assert missing.status == "UNHEALTHY"
    assert "WRITER_COUNT_MISSING" in missing.reasons


def test_output_and_numeric_bounds_fail_closed() -> None:
    with pytest.raises(AuthoritativeSchedulerHealthError, match="PROBE_OUTPUT_BOUND_EXCEEDED"):
        _observation(output="x" * 4_097)
    with pytest.raises(AuthoritativeSchedulerHealthError, match="PROBE_BOUND_INVALID"):
        probe_authoritative_scheduler_health(_observation(), max_duration_milliseconds=True)
    with pytest.raises(AuthoritativeSchedulerHealthError, match="OBSERVATION_FIELD_INVALID"):
        _observation(writers=-1.5)


def test_observation_result_and_safety_tampering_fail_closed() -> None:
    observation = _observation()
    with pytest.raises(AuthoritativeSchedulerHealthError, match="OBSERVATION_HASH_MISMATCH"):
        probe_authoritative_scheduler_health(replace(observation, observed_writer_count=2))
    evidence = probe_authoritative_scheduler_health(observation)
    with pytest.raises(AuthoritativeSchedulerHealthError, match="EVIDENCE_HASH_MISMATCH"):
        validate_scheduler_health_evidence(replace(evidence, evidence_hash="0" * 64))
    with pytest.raises(AuthoritativeSchedulerHealthError, match="EVIDENCE_SAFETY_BOUNDARY_INVALID"):
        validate_scheduler_health_evidence(replace(evidence, recovery_authorized=True))


def test_probe_has_no_subprocess_service_notification_or_mutation_surface() -> None:
    names = set(probe_authoritative_scheduler_health.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _observation(
    *,
    unit=AUTHORITATIVE_UNIT,
    load="loaded",
    active="active",
    sub="running",
    pid=123,
    writers=1,
    complete=True,
    duration=25,
    age=1,
    output="healthy",
):
    return make_scheduler_health_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        duration_milliseconds=duration,
        unit_name=unit,
        load_state=load,
        active_state=active,
        sub_state=sub,
        main_pid=pid,
        observed_writer_count=writers,
        complete=complete,
        probe_name="systemctl-user-show-v1",
        source_identity_hash="a" * 64,
        output=output,
    )
