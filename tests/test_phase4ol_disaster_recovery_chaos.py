from __future__ import annotations

import copy

from scripts.local.phase4ol_disaster_recovery_chaos import (
    SCENARIOS,
    build_passing_matrix,
    certify_chaos_matrix,
)


def test_complete_ordered_chaos_matrix_meets_logical_rto() -> None:
    result = certify_chaos_matrix(build_passing_matrix())
    assert result["verdict"] == "PASS"
    assert result["scenario_count"] == len(SCENARIOS) == 9
    assert result["total_logical_steps"] == result["total_step_budget"] == 38
    assert result["final_state"] == "RECOVERED"


def test_all_degraded_intervals_are_frozen_and_capability_disabled() -> None:
    rows = build_passing_matrix()
    assert all(row["state"] == "FROZEN" for row in rows[:-1])
    assert all(row["capabilities_allowed"] is False for row in rows[:-1])
    assert rows[-1]["state"] == "RECOVERED"


def test_skipped_reordered_or_duplicated_transition_refuses() -> None:
    rows = build_passing_matrix()
    candidates = [rows[:-1], [rows[1], rows[0], *rows[2:]], [rows[0], rows[0], *rows[2:]]]
    for candidate in candidates:
        result = certify_chaos_matrix(candidate)
        assert result["verdict"] == "REFUSE"
        assert result["final_state"] == "FROZEN"
        assert "SCENARIO_SEQUENCE_INCOMPLETE_OR_REORDERED" in result["errors"]


def test_step_budget_state_and_degraded_capability_violations_refuse() -> None:
    mutations = []
    over_budget = build_passing_matrix()
    over_budget[2]["logical_steps"] = 5
    mutations.append((over_budget, "RESTART_REPLAY:RTO_BUDGET_EXCEEDED"))
    wrong_state = build_passing_matrix()
    wrong_state[3]["state"] = "RECOVERED"
    mutations.append((wrong_state, "CHECKPOINT_LOSS:STATE_TRANSITION_INVALID"))
    exposed = build_passing_matrix()
    exposed[4]["capabilities_allowed"] = True
    mutations.append((exposed, "SINGLE_COPY_CORRUPTION:DEGRADED_CAPABILITY_EXPOSURE"))
    for rows, expected in mutations:
        result = certify_chaos_matrix(rows)
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_tampered_or_missing_proof_and_settlement_drift_refuse() -> None:
    mutations = []
    tampered = build_passing_matrix()
    tampered[0]["logical_steps"] = 1
    mutations.append((tampered, "QUORUM_LOSS:RECORD_HASH_MISMATCH"))
    missing = build_passing_matrix()
    missing[1]["proof_sha256"] = ""
    mutations.append((missing, "FREEZE_COMMIT:PROOF_MISSING"))
    drift = build_passing_matrix()
    drift[2]["blocked_on_september_1_settlement"] = "removed"
    mutations.append((drift, "RESTART_REPLAY:SETTLEMENT_BLOCKER_DRIFT"))
    for rows, expected in mutations:
        result = certify_chaos_matrix(rows)
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_certificate_is_deterministic_input_preserving_and_execution_free() -> None:
    rows = build_passing_matrix()
    original = copy.deepcopy(rows)
    first = certify_chaos_matrix(rows)
    second = certify_chaos_matrix(rows)
    assert first == second
    assert rows == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
