import json

from kalshi_predictor.benchmarking.drawdown_guard import (
    ORDERINGS,
    PER_CATEGORY_CAPS,
    POSITION_SCALES,
    build_drawdown_aware_guard_refinement,
    write_drawdown_aware_guard_refinement,
)


def test_pmb24_is_deterministic_local_and_preview_only(tmp_path):
    first = json.loads(write_drawdown_aware_guard_refinement(tmp_path / "a").read_text())
    second = json.loads(write_drawdown_aware_guard_refinement(tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["runtime_policy_changed"] is False


def test_pmb24_evaluates_ordering_and_cap_grid_without_outcome_selection():
    report = build_drawdown_aware_guard_refinement()
    assert report["summary"]["candidate_count"] == (
        len(ORDERINGS) * len(PER_CATEGORY_CAPS) * len(POSITION_SCALES)
    )
    assert report["summary"]["all_candidates_settlement_blind"] is True
    assert all(not row["selection_uses_settlement_outcomes"] for row in report["candidates"])


def test_pmb24_recommendation_meets_both_qualification_requirements():
    report = build_drawdown_aware_guard_refinement()
    recommendation = report["recommended_preview"]
    assert recommendation is not None
    assert recommendation["return_on_capital_preserved"] is True
    assert recommendation["drawdown_regression_prevented"] is True
    assert recommendation["qualifies"] is True
