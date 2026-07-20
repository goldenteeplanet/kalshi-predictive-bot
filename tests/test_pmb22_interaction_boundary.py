import json
from pathlib import Path

from kalshi_predictor.benchmarking.interaction_boundary import (
    FORECAST_BIAS_GRID,
    SPREAD_ADDITION_GRID,
    build_interaction_boundary_refinement,
    golden_interaction_boundary_summary,
    write_interaction_boundary_refinement,
)

GOLDEN = Path(__file__).parent / "golden" / "pmb22_interaction_boundary_summary.json"


def test_pmb22_is_deterministic_local_and_execution_disabled(tmp_path):
    first = json.loads(write_interaction_boundary_refinement(tmp_path / "a").read_text())
    second = json.loads(write_interaction_boundary_refinement(tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_retrained"] is False


def test_pmb22_finds_minimum_break_and_certifies_lower_buffer():
    report = build_interaction_boundary_refinement()
    assert report["summary"]["cells"] == len(FORECAST_BIAS_GRID) * len(SPREAD_ADDITION_GRID)
    assert report["summary"]["boundary_found"] is True
    assert report["minimum_joint_only_break"]["joint_only_break"] is True
    assert report["deterministic_safety_buffer"]["certified_within_grid"] is True
    assert report["summary"]["all_attribution_complete"] is True


def test_pmb22_matches_golden_summary():
    expected = json.loads(GOLDEN.read_text())
    assert golden_interaction_boundary_summary(build_interaction_boundary_refinement()) == expected
