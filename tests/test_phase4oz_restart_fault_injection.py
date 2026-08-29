from __future__ import annotations

import copy

import pytest

from scripts.local.phase4oy_wsl_restart_rehearsal import build_passing_trace, certify_rehearsal
from scripts.local.phase4oz_restart_fault_injection import FAULTS, inject_fault, run_fault_campaign


def test_all_faults_are_observable_deterministic_safe_refusals() -> None:
    result = run_fault_campaign()
    assert result["verdict"] == "PASS"
    assert result["case_count"] == 76
    assert len(result["fault_classes"]) == len(FAULTS) == 10
    assert result["silent_faults"] == []
    assert result["zero_silent_invariant_violations"] is True
    assert all(
        row["observable"] and row["safe_refusal"] and row["deterministic"]
        for row in result["results"]
    )


def test_kill_switch_loss_and_execution_drift_are_never_silent() -> None:
    trace = build_passing_trace()
    for fault in ("KILL_SWITCH_LOSS", "INVARIANT_DRIFT"):
        for stage in FAULTS[fault]:
            result = certify_rehearsal(inject_fault(trace, fault, stage))
            assert result["verdict"] == "REFUSE"
            assert result["final_health"] == "UNHEALTHY_FROZEN"
            assert any("EXECUTION_INVARIANT_VIOLATION" in error for error in result["errors"])


def test_service_crash_false_health_stale_checkpoint_and_storm_are_observed() -> None:
    trace = build_passing_trace()
    cases = (
        ("SERVICE_CRASH", 6, "BOT_SERVICE_MISSING"),
        ("FALSE_HEALTH", 4, "STARTUP_ORDER_INVALID"),
        ("CHECKPOINT_STALENESS", 5, "STALE_CHECKPOINT"),
        ("RESTART_STORM", 2, "RESTART_LOOP_LIMIT_EXCEEDED"),
    )
    for fault, stage, fragment in cases:
        result = certify_rehearsal(inject_fault(trace, fault, stage))
        assert any(fragment in error for error in result["errors"])


def test_omission_duplication_delay_and_telemetry_loss_are_observed() -> None:
    trace = build_passing_trace()
    cases = (
        ("TRANSITION_OMISSION", 0, "STARTUP_ORDER_INVALID"),
        ("TRANSITION_DUPLICATION", 1, "STARTUP_ORDER_INVALID"),
        ("TRANSITION_DELAY", 2, "SEQUENCE_INVALID"),
        ("TELEMETRY_LOSS", 3, "TRANSITION_HASH_MISMATCH"),
    )
    for fault, stage, fragment in cases:
        result = certify_rehearsal(inject_fault(trace, fault, stage))
        assert any(fragment in error for error in result["errors"])


def test_inapplicable_fault_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="not applicable"):
        inject_fault(build_passing_trace(), "SERVICE_CRASH", 0)


def test_campaign_is_deterministic_input_preserving_and_execution_free() -> None:
    trace = build_passing_trace()
    original = copy.deepcopy(trace)
    inject_fault(trace, "KILL_SWITCH_LOSS", 0)
    assert trace == original
    first = run_fault_campaign()
    assert first == run_fault_campaign()
    assert first["actual_restart_performed"] is False
    assert first["executable"] is False
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
