from __future__ import annotations

from kalshi_predictor.r5_recovery8 import consolidate_three_cycle_census


def _cycle(index: int, **updates):
    evidence = {
        "runtime_seconds": 100 + index,
        "memory_peak_bytes": 1_000 + index,
        "r3_heartbeat_max_age_seconds": 15,
        "r5_heartbeat_max_age_seconds": 15,
        "memory_events": {"oom": 0, "oom_kill": 0},
        "actual_output_sha256": "same-hash",
        "actual_row_count": 10,
        "writer_clear_after": True,
        "locks_clear_after": True,
        "execution_enabled": False,
    }
    evidence.update(updates)
    return {"cycle_id": f"cycle-{index}", "status": "PASSED", "evidence": evidence}


def _census(*cycles):
    return {"report_sha256": "source-hash", "cycle_assessments": list(cycles)}


def test_three_exact_passing_cycles_certify() -> None:
    report = consolidate_three_cycle_census(_census(_cycle(1), _cycle(2), _cycle(3)))
    assert report["status"] == "PASSED"
    assert all(report["gates"].values())
    assert report["metrics"]["runtime_seconds"]["delta_first_to_last"] == 2


def test_fewer_than_three_fails_closed() -> None:
    report = consolidate_three_cycle_census(_census(_cycle(1), _cycle(2)))
    assert report["status"] == "FAILED"
    assert report["gates"]["all_cycles_passed"] is False


def test_duplicate_cycle_ids_fail_closed() -> None:
    duplicate = _cycle(2)
    duplicate["cycle_id"] = "cycle-1"
    report = consolidate_three_cycle_census(_census(_cycle(1), duplicate, _cycle(3)))
    assert report["gates"]["all_cycles_passed"] is False


def test_parity_and_safety_failures_are_attributed() -> None:
    bad = _cycle(
        3,
        actual_output_sha256="different",
        actual_row_count=9,
        execution_enabled=True,
        locks_clear_after=False,
        r5_heartbeat_max_age_seconds=31,
    )
    report = consolidate_three_cycle_census(_census(_cycle(1), _cycle(2), bad))
    assert report["status"] == "FAILED"
    for gate in (
        "execution_disabled",
        "heartbeat_fresh",
        "output_hash_parity",
        "row_count_parity",
        "writer_and_locks_clear",
    ):
        assert report["gates"][gate] is False


def test_oom_fails_closed() -> None:
    report = consolidate_three_cycle_census(
        _census(_cycle(1), _cycle(2), _cycle(3, memory_events={"oom": 1}))
    )
    assert report["gates"]["no_oom"] is False


def test_report_is_deterministic() -> None:
    census = _census(_cycle(1), _cycle(2), _cycle(3))
    assert consolidate_three_cycle_census(census) == consolidate_three_cycle_census(census)
