from __future__ import annotations

import copy

import pytest

from scripts.local.phase4om_recovery_tail_soak import run_soak, verify_soak


def test_seeded_256_run_soak_passes_all_tail_budgets() -> None:
    report = run_soak(seed=20260829)
    result = verify_soak(report)
    assert result["verdict"] == "PASS"
    assert report["run_count"] == 256
    assert report["unsafe_run_count"] == 0
    assert report["metrics"]["p99"] <= report["aggregate_tail_budget"]
    assert all(row["p99"] <= row["tail_budget"] for row in report["scenario_tails"].values())


def test_identical_seed_is_byte_deterministic_and_different_seed_changes_proof() -> None:
    first = run_soak(seed=7)
    assert first == run_soak(seed=7)
    assert first["soak_sha256"] != run_soak(seed=8)["soak_sha256"]


def test_percentile_total_and_tail_manipulation_refuse() -> None:
    report = run_soak(seed=11)
    variants = []
    metric = copy.deepcopy(report)
    metric["metrics"]["p99"] -= 1
    variants.append((metric, "PERCENTILE_METRICS_INVALID"))
    total = copy.deepcopy(report)
    total["runs"][0]["total_cost"] -= 1
    variants.append((total, "TOTAL_COST_MISMATCH"))
    tail = copy.deepcopy(report)
    tail["scenario_tails"]["QUORUM_LOSS"]["p99"] = 1
    variants.append((tail, "SCENARIO_TAILS_INVALID"))
    for candidate, expected in variants:
        result = verify_soak(candidate)
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_unsafe_run_reordering_and_scenario_omission_refuse() -> None:
    report = run_soak(seed=13)
    unsafe = copy.deepcopy(report)
    unsafe["runs"][0]["unsafe"] = True
    assert "UNSAFE_RUN_DETECTED" in verify_soak(unsafe)["errors"]
    reordered = copy.deepcopy(report)
    reordered["runs"][0], reordered["runs"][1] = reordered["runs"][1], reordered["runs"][0]
    assert "RUN_SEQUENCE_INVALID" in verify_soak(reordered)["errors"]
    omitted = copy.deepcopy(report)
    del omitted["runs"][0]["scenario_costs"]["QUORUM_LOSS"]
    assert "SCENARIO_COSTS_INVALID" in verify_soak(omitted)["errors"]


def test_short_soak_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 100"):
        run_soak(seed=1, run_count=99)


def test_verification_is_input_preserving_and_execution_free() -> None:
    report = run_soak(seed=17)
    original = copy.deepcopy(report)
    result = verify_soak(report)
    assert report == original
    assert result["safety"]["offline_only"] is True
    assert all(value is False for key, value in result["safety"].items() if key != "offline_only")
