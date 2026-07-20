from __future__ import annotations

from kalshi_predictor.r5_recovery6a import EvidenceLimits, assess_cycle, run_census

LIMITS = EvidenceLimits(runtime_seconds=100, memory_peak_bytes=1_000, heartbeat_age_seconds=30)


def cycle(cycle_id: str, **updates):
    row = {
        "cycle_id": cycle_id,
        "started_at": "2026-07-18T00:00:00Z",
        "ended_at": "2026-07-18T00:01:00Z",
        "systemd": "Result=success\nExecMainStatus=0\nMemoryCurrent=500\nMemoryPeak=800\n",
        "runtime_seconds": 60,
        "memory_events": "high 0\noom 0\noom_kill 0\n",
        "r3_heartbeat_max_age_seconds": 16,
        "r5_heartbeat_max_age_seconds": 16,
        "expected_output_sha256": "abc",
        "actual_output_sha256": "abc",
        "expected_row_count": 10,
        "actual_row_count": 10,
        "writer_clear_after": True,
        "locks_clear_after": True,
        "execution_enabled": False,
    }
    row.update(updates)
    return row


def test_parses_systemd_and_memory_events() -> None:
    result = assess_cycle(cycle("one"), LIMITS)
    assert result["status"] == "PASSED"
    assert result["evidence"]["memory_peak_bytes"] == 800
    assert result["evidence"]["memory_events"]["oom"] == 0


def test_each_stop_gate_fails_closed() -> None:
    cases = {
        "timeout": {"runtime_seconds": 101},
        "oom": {"memory_events": "oom 1\noom_kill 0"},
        "stale_heartbeat": {"r5_heartbeat_max_age_seconds": 31},
        "output_mismatch": {"actual_output_sha256": "different"},
        "row_count_mismatch": {"actual_row_count": 9},
        "writer_contention": {"writer_clear_after": False},
        "lock_contention": {"locks_clear_after": False},
        "execution_enabled": {"execution_enabled": True},
    }
    for expected, updates in cases.items():
        assert expected in assess_cycle(cycle(expected, **updates), LIMITS)["failures"]


def test_census_stops_immediately_after_first_failure() -> None:
    report = run_census(
        [cycle("one"), cycle("two", actual_output_sha256="bad"), cycle("three")],
        limits=LIMITS,
    )
    assert report["status"] == "FAILED"
    assert [row["cycle_id"] for row in report["cycle_assessments"]] == ["one", "two"]
    assert report["stopped_at_cycle"] == "two"


def test_three_distinct_cycles_pass_deterministically() -> None:
    first = run_census([cycle("one"), cycle("two"), cycle("three")], limits=LIMITS)
    second = run_census([cycle("one"), cycle("two"), cycle("three")], limits=LIMITS)
    assert first == second
    assert first["status"] == "PASSED"
    assert first["decision"] == "CERTIFIED_THREE_CYCLES"


def test_resume_ignores_duplicate_and_does_not_repeat_cycle() -> None:
    initial = run_census([cycle("one")], limits=LIMITS)
    resumed = run_census(
        [cycle("one"), cycle("two"), cycle("three")],
        previous_report=initial,
        limits=LIMITS,
    )
    assert resumed["status"] == "PASSED"
    assert [row["cycle_id"] for row in resumed["cycle_assessments"]] == [
        "one",
        "two",
        "three",
    ]
    assert resumed["duplicate_cycle_ids_ignored"] == ["one"]


def test_resume_after_failure_never_consumes_new_cycle() -> None:
    failed = run_census([cycle("one", locks_clear_after=False)], limits=LIMITS)
    resumed = run_census([cycle("two")], previous_report=failed, limits=LIMITS)
    assert len(resumed["cycle_assessments"]) == 1
    assert resumed["status"] == "FAILED"
