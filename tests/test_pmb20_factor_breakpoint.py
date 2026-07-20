import json

from kalshi_predictor.benchmarking.factor_breakpoint import (
    FACTOR_GRIDS,
    build_factor_isolated_breakpoint_attribution,
    write_factor_isolated_breakpoint_attribution,
)


def test_pmb20_is_deterministic_local_and_frozen(tmp_path):
    first = json.loads(write_factor_isolated_breakpoint_attribution(tmp_path / "a").read_text())
    second = json.loads(write_factor_isolated_breakpoint_attribution(tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_retrained"] is False


def test_pmb20_isolates_all_four_requested_factors():
    report = build_factor_isolated_breakpoint_attribution()
    assert {row["factor"] for row in report["factors"]} == set(FACTOR_GRIDS)
    assert report["summary"]["factor_count"] == 4
    assert report["summary"]["evaluated_levels"] == sum(map(len, FACTOR_GRIDS.values()))
    assert report["summary"]["all_attribution_complete"] is True


def test_pmb20_reports_exact_individual_break_or_grid_survival():
    report = build_factor_isolated_breakpoint_attribution()
    for factor in report["factors"]:
        assert factor["first_any_break"] is not None or factor[
            "bounded_grid_preserved_strict_advantage"
        ] is True
        if factor["first_any_break"] is not None:
            assert factor["first_any_break"]["step"] > 0
