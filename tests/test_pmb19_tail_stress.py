import json

from kalshi_predictor.benchmarking.tail_stress import (
    TAIL_STRESS_LADDER,
    build_tail_stress_breakpoint_search,
    write_tail_stress_breakpoint_search,
)


def test_pmb19_is_deterministic_local_and_frozen(tmp_path):
    first = json.loads(write_tail_stress_breakpoint_search(tmp_path / "a").read_text())
    second = json.loads(write_tail_stress_breakpoint_search(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["levels"] == len(TAIL_STRESS_LADDER)
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_retrained"] is False


def test_pmb19_stress_ladder_is_monotonic_and_bounded():
    levels = TAIL_STRESS_LADDER
    assert [row["severity"] for row in levels] == list(range(len(levels)))
    assert all(float(row["forecast_bias"]) >= -0.10 for row in levels)
    assert all(float(row["spread_addition"]) <= 0.16 for row in levels)
    assert all(float(row["depth_multiplier"]) >= 0.10 for row in levels)


def test_pmb19_reports_exact_breakpoint_or_bounded_exhaustion():
    report = build_tail_stress_breakpoint_search()
    breakpoints = report["breakpoints"]
    assert breakpoints["first_any_break"]["severity"] == 1
    assert breakpoints["first_drawdown_break"]["severity"] == 1
    assert breakpoints["first_capital_efficiency_break"]["severity"] == 1
    assert breakpoints["bounded_search_exhausted_without_break"] is False
    assert report["summary"]["all_attribution_complete"] is True
    for level in report["stress_levels"]:
        assert "risk_adjusted_capital_efficiency" in level["baseline"]
        assert "risk_adjusted_capital_efficiency" in level["robust_policy"]
