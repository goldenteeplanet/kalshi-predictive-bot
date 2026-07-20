import json

from kalshi_predictor.benchmarking.stress_guard import (
    build_stress_aware_allocation_guard_preview,
    write_stress_aware_allocation_guard_preview,
)


def test_pmb23_is_deterministic_local_and_preview_only(tmp_path):
    first = json.loads(write_stress_aware_allocation_guard_preview(tmp_path / "a").read_text())
    second = json.loads(write_stress_aware_allocation_guard_preview(tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["runtime_policy_changed"] is False


def test_pmb23_applies_exact_certified_buffer():
    report = build_stress_aware_allocation_guard_preview()
    assert report["certified_buffer"]["maximum_forecast_bias_magnitude"] == "0.008"
    assert report["certified_buffer"]["maximum_spread_addition"] == "0.008"
    assert report["summary"]["inside_buffer_rows"] == 75
    assert report["summary"]["outside_buffer_rows"] == 33
    assert report["summary"]["all_attribution_complete"] is True


def test_pmb23_guard_reports_capital_benefit_and_drawdown_tradeoff():
    report = build_stress_aware_allocation_guard_preview()
    assert report["guarded_policy"]["trade_count"] <= report["baseline"]["trade_count"]
    assert float(report["guarded_policy"]["capital_usage"]) <= float(
        report["baseline"]["capital_usage"]
    )
    assert report["comparison"]["capital_usage_reduced_or_equal"] is True
    assert report["comparison"]["return_on_capital_improved_or_equal"] is True
    assert report["comparison"]["drawdown_improved_or_equal"] is False
    assert float(report["comparison"]["max_drawdown_delta"]) > 0
    assert report["comparison"]["otherwise_allocating_rows_rejected_by_guard"] > 0
