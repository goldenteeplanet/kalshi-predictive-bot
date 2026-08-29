from __future__ import annotations

import copy

from scripts.local.phase4oy_wsl_restart_rehearsal import (
    TRANSITIONS,
    build_passing_trace,
    certify_rehearsal,
)


def test_complete_offline_restart_trace_certifies() -> None:
    result = certify_rehearsal(build_passing_trace())
    assert result["verdict"] == "PASS"
    assert result["transition_count"] == len(TRANSITIONS) == 9
    assert result["final_health"] == "HEALTHY"
    assert result["actual_restart_performed"] is False


def test_execution_invariants_hold_at_every_transition() -> None:
    for row in build_passing_trace():
        invariants = row["invariants"]
        assert invariants["paper_kill_switch"] is True
        assert all(
            value is False for key, value in invariants.items() if key != "paper_kill_switch"
        )


def test_unsafe_startup_order_or_transient_execution_enablement_refuses() -> None:
    reordered = build_passing_trace()
    reordered[3], reordered[4] = reordered[4], reordered[3]
    assert "STARTUP_ORDER_INVALID" in certify_rehearsal(reordered)["errors"]
    unsafe = build_passing_trace()
    unsafe[2]["invariants"]["paper_order_creation"] = True
    result = certify_rehearsal(unsafe)
    assert result["verdict"] == "REFUSE"
    assert "UBUNTU_RELAUNCHED:EXECUTION_INVARIANT_VIOLATION" in result["errors"]


def test_service_started_early_missing_service_or_premature_health_refuses() -> None:
    early = build_passing_trace()
    early[3]["bot_active"] = True
    assert "SYSTEMD_READY:BOT_STARTED_BEFORE_VERIFICATION" in certify_rehearsal(early)["errors"]
    missing = build_passing_trace()
    missing[-1]["ui_active"] = False
    assert "HEALTH_CLAIM_BEFORE_SERVICES_ACTIVE" in certify_rehearsal(missing)["errors"]


def test_stale_checkpoint_restart_loop_and_tamper_refuse() -> None:
    stale = build_passing_trace()
    stale[6]["checkpoint_fresh"] = False
    assert "BOT_ACTIVE:STALE_CHECKPOINT" in certify_rehearsal(stale)["errors"]
    looping = build_passing_trace()
    looping[2]["restart_attempt"] = 4
    assert "UBUNTU_RELAUNCHED:RESTART_LOOP_LIMIT_EXCEEDED" in certify_rehearsal(looping)["errors"]
    tampered = build_passing_trace()
    tampered[0]["sequence"] = 2
    assert "PROCESS_CRASH:TRANSITION_HASH_MISMATCH" in certify_rehearsal(tampered)["errors"]


def test_rehearsal_is_deterministic_input_preserving_and_execution_free() -> None:
    trace = build_passing_trace()
    original = copy.deepcopy(trace)
    first = certify_rehearsal(trace)
    second = certify_rehearsal(trace)
    assert first == second
    assert trace == original
    assert first["executable"] is False
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
