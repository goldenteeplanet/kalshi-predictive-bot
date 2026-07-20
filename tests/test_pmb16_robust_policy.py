import json

from kalshi_predictor.benchmarking.robust_policy import (
    build_robust_zone_policy_comparison,
    write_robust_zone_policy_comparison,
)


def test_pmb16_is_deterministic_and_uses_only_robust_allocation_zones(tmp_path):
    first = json.loads(write_robust_zone_policy_comparison(tmp_path / "a").read_text())
    second = json.loads(write_robust_zone_policy_comparison(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["scenario_count"] == 81
    assert first["summary"]["robust_policy_allocates_only_robust_zones"] is True
    assert first["summary"]["all_attribution_complete"] is True
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False


def test_pmb16_robust_policy_reduces_trades_and_capital_usage():
    report = build_robust_zone_policy_comparison()
    assert report["robust_policy"]["trade_count"] < report["baseline"]["trade_count"]
    assert float(report["robust_policy"]["capital_usage"]) < float(
        report["baseline"]["capital_usage"]
    )
    assert report["comparison"]["robust_zone_filtered_trades"] > 0


def test_pmb16_reports_pnl_drawdown_and_rejected_opportunity_deltas():
    report = build_robust_zone_policy_comparison()
    comparison = report["comparison"]
    assert set(comparison) == {
        "trade_count_delta", "rejected_opportunity_delta", "capital_usage_delta",
        "pnl_delta", "max_drawdown_delta", "robust_zone_filtered_trades",
    }
    assert comparison["rejected_opportunity_delta"] > 0
