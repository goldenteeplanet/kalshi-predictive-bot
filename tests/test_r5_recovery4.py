from __future__ import annotations

from kalshi_predictor.r5_recovery4 import (
    R5CertificationLimits,
    assess_cycle,
    build_certification_report,
)

LIMITS = R5CertificationLimits(
    runtime_seconds=100, memory_peak_bytes=1_000, heartbeat_age_seconds=30
)


def cycle(cycle_id: str, **updates):
    row = {
        "cycle_id": cycle_id,
        "runtime_seconds": 50,
        "memory_current_end_bytes": 500,
        "memory_peak_bytes": 800,
        "memory_high_events": 0,
        "oom_events": 0,
        "oom_kill_events": 0,
        "heartbeat_max_age_seconds": 15,
        "output_parity": True,
        "writer_clear_after": True,
        "locks_clear_after": True,
        "execution_enabled": False,
    }
    row.update(updates)
    return row


def test_three_distinct_cycles_pass_deterministically() -> None:
    report1 = build_certification_report(
        baseline={"runtime_seconds": 200, "memory_peak_bytes": 2_000},
        cycles=[cycle("a"), cycle("b"), cycle("c")],
        limits=LIMITS,
    )
    report2 = build_certification_report(
        baseline={"runtime_seconds": 200, "memory_peak_bytes": 2_000},
        cycles=[cycle("a"), cycle("b"), cycle("c")],
        limits=LIMITS,
    )
    assert report1 == report2
    assert report1["status"] == "PASSED"
    assert report1["comparison"]["runtime_improvement_percent"] == 75.0
    assert report1["rollback_recommendation"]["action"] == "ADVANCE_TO_R5_RECOVERY_5_PREVIEW"


def test_timeout_fails_closed() -> None:
    result = assess_cycle(cycle("a", runtime_seconds=101), LIMITS)
    assert result["failures"] == ["timeout"]


def test_oom_fails_closed() -> None:
    result = assess_cycle(cycle("a", oom_events=1), LIMITS)
    assert result["failures"] == ["oom"]


def test_stale_heartbeat_fails_closed() -> None:
    result = assess_cycle(cycle("a", heartbeat_max_age_seconds=31), LIMITS)
    assert result["failures"] == ["stale_heartbeat"]


def test_lock_contention_fails_closed() -> None:
    result = assess_cycle(cycle("a", locks_clear_after=False), LIMITS)
    assert result["failures"] == ["lock_contention"]


def test_output_mismatch_fails_closed() -> None:
    result = assess_cycle(cycle("a", output_parity=False), LIMITS)
    assert result["failures"] == ["output_mismatch"]


def test_execution_enablement_has_highest_failure_priority() -> None:
    result = assess_cycle(
        cycle("a", execution_enabled=True, oom_events=1, timed_out=True), LIMITS
    )
    assert result["failures"][:3] == ["execution_enabled", "oom", "timeout"]


def test_duplicate_cycle_ids_do_not_complete_census() -> None:
    report = build_certification_report(
        baseline={}, cycles=[cycle("a"), cycle("a"), cycle("b")], limits=LIMITS
    )
    assert report["status"] == "WAITING"
    assert report["three_cycle_census"]["observed_distinct_cycles"] == 2
    assert report["rollback_recommendation"]["action"] == "HOLD_FOR_MORE_CYCLES"
