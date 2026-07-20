from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.r5_recovery5 import (
    SchedulerPolicy,
    build_certification_report,
    simulate_scheduler,
)


def trigger(event_id: str, run_id: str, **updates):
    event = {
        "event_id": event_id,
        "kind": "timer",
        "run_id": run_id,
        "writer_safe": True,
        "locks_clear": True,
        "execution_enabled": False,
    }
    event.update(updates)
    return event


def test_overlap_is_blocked_and_only_one_run_starts() -> None:
    result = simulate_scheduler([trigger("t1", "run-1"), trigger("t2", "run-2")])
    assert result["started_runs"] == ["run-1"]
    assert result["decisions"][1]["reason"] == "service already active"
    assert result["sole_writer_preserved"] is True


def test_writer_or_lock_contention_blocks_start() -> None:
    result = simulate_scheduler([trigger("t1", "run-1", writer_safe=False)])
    assert result["started_runs"] == []
    assert result["decisions"][0]["action"] == "BLOCK"


def test_missed_schedule_is_not_replayed_by_default() -> None:
    event = trigger("m1", "run-1") | {"kind": "missed_timer_replay"}
    result = simulate_scheduler([event])
    assert result["decisions"][0]["reason"] == "persistent replay disabled"


def test_restart_requires_fresh_evidence() -> None:
    event = trigger("r1", "run-1", heartbeat_age_seconds=31) | {"kind": "restart_recovery"}
    result = simulate_scheduler([event])
    assert result["started_runs"] == []
    assert result["decisions"][0]["reason"] == "restart evidence is stale"


def test_valid_restart_can_complete_once() -> None:
    events = [
        trigger("r1", "run-1", heartbeat_age_seconds=15) | {"kind": "restart_recovery"},
        {"event_id": "c1", "kind": "complete", "run_id": "run-1", "output_valid": True},
        trigger("t2", "run-1"),
    ]
    result = simulate_scheduler(events)
    assert result["completed_runs"] == ["run-1"]
    assert result["decisions"][2]["reason"] == "run already completed"


def test_timeout_and_oom_fail_closed() -> None:
    timeout = simulate_scheduler([trigger("t1", "run-1"), {"kind": "timeout", "run_id": "run-1"}])
    oom = simulate_scheduler([trigger("t1", "run-1"), {"kind": "oom", "run_id": "run-1"}])
    assert timeout["violations"] == ["timeout"]
    assert oom["violations"] == ["oom"]
    assert timeout["active_run_at_end"] is None
    assert oom["active_run_at_end"] is None


def test_execution_enablement_always_blocks() -> None:
    result = simulate_scheduler([trigger("t1", "run-1")], SchedulerPolicy(execution_enabled=True))
    assert result["started_runs"] == []
    assert result["violations"] == ["execution_enabled"]


def test_full_fixture_passes_deterministically() -> None:
    path = Path("reports/phase_r5_recovery5/synthetic_scheduler_scenarios.json")
    scenarios = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    first = build_certification_report(scenarios)
    second = build_certification_report(scenarios)
    assert first == second
    assert first["status"] == "PASSED_LOCAL_PREVIEW"
    assert first["rollback_recommendation"] == "READY_FOR_GUARDED_SCHEDULER_REENTRY_CERTIFICATION"
