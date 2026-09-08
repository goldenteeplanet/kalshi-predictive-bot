from __future__ import annotations

import copy

import pytest

from scripts.local.phase4om_recovery_tail_soak import run_soak
from scripts.local.phase4on_tail_regression_gate import (
    compare_candidate,
    create_baseline,
    promote_baseline,
)


def _compare(baseline_seed=3, candidate_seed=7, **overrides):
    baseline_report = run_soak(seed=baseline_seed)
    baseline = create_baseline(baseline_report, version=1)
    candidate = run_soak(seed=candidate_seed)
    values = {
        "trusted_baseline_sha256": baseline["baseline_sha256"],
        "absolute_tolerance": 0,
        "relative_tolerance": 0.0,
    }
    values.update(overrides)
    return baseline, candidate, compare_candidate(baseline, candidate, **values)


def test_improved_candidate_passes_and_promotes_version() -> None:
    baseline, candidate, result = _compare()
    assert result["verdict"] == "PASS"
    assert result["promotion_allowed"] is True
    assert result["classifications"]["p99"] == "IMPROVEMENT"
    promoted = promote_baseline(result, candidate)
    assert promoted["version"] == baseline["version"] + 1 == 2
    assert promoted["soak_sha256"] == candidate["soak_sha256"]


def test_zero_tolerance_detects_p99_and_worst_regression() -> None:
    _, _, result = _compare(baseline_seed=7, candidate_seed=3)
    assert result["verdict"] == "REFUSE"
    assert "P99_REGRESSION" in result["errors"]
    assert result["promotion_allowed"] is False


def test_absolute_or_relative_tolerance_can_bound_small_change() -> None:
    _, _, absolute = _compare(
        baseline_seed=7, candidate_seed=3, absolute_tolerance=2, relative_tolerance=0.0
    )
    assert absolute["verdict"] == "PASS"
    _, _, relative = _compare(
        baseline_seed=7, candidate_seed=3, absolute_tolerance=0, relative_tolerance=0.04
    )
    assert relative["verdict"] == "PASS"


def test_tampered_baseline_candidate_sample_and_seed_reuse_refuse() -> None:
    baseline, candidate, _ = _compare()
    tampered = copy.deepcopy(baseline)
    tampered["metrics"]["p99"] = 1
    result = compare_candidate(
        tampered,
        candidate,
        trusted_baseline_sha256=baseline["baseline_sha256"],
        absolute_tolerance=0,
        relative_tolerance=0,
    )
    assert "BASELINE_HASH_MISMATCH" in result["errors"]
    same_seed = run_soak(seed=baseline["seed"])
    result = compare_candidate(
        baseline,
        same_seed,
        trusted_baseline_sha256=baseline["baseline_sha256"],
        absolute_tolerance=0,
        relative_tolerance=0,
    )
    assert "CANDIDATE_SEED_REUSED" in result["errors"]
    short = run_soak(seed=9, run_count=128)
    result = compare_candidate(
        baseline,
        short,
        trusted_baseline_sha256=baseline["baseline_sha256"],
        absolute_tolerance=10,
        relative_tolerance=1,
    )
    assert "SAMPLE_SIZE_CHANGED" in result["errors"]


def test_invalid_or_unsafe_candidate_cannot_promote() -> None:
    baseline, candidate, _ = _compare()
    unsafe = copy.deepcopy(candidate)
    unsafe["runs"][0]["unsafe"] = True
    result = compare_candidate(
        baseline,
        unsafe,
        trusted_baseline_sha256=baseline["baseline_sha256"],
        absolute_tolerance=100,
        relative_tolerance=1,
    )
    assert result["promotion_allowed"] is False
    assert "CANDIDATE_SOAK_INVALID" in result["errors"]
    with pytest.raises(ValueError, match="not eligible"):
        promote_baseline(result, unsafe)


def test_comparison_is_deterministic_input_preserving_and_execution_free() -> None:
    baseline_report = run_soak(seed=3)
    baseline = create_baseline(baseline_report, version=1)
    candidate = run_soak(seed=7)
    original = copy.deepcopy((baseline, candidate))
    kwargs = {
        "trusted_baseline_sha256": baseline["baseline_sha256"],
        "absolute_tolerance": 0,
        "relative_tolerance": 0,
    }
    first = compare_candidate(baseline, candidate, **kwargs)
    assert first == compare_candidate(baseline, candidate, **kwargs)
    assert (baseline, candidate) == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
