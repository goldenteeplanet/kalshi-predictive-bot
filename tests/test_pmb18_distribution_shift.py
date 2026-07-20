import json

from kalshi_predictor.benchmarking.distribution_shift import (
    SHIFT_REGIMES,
    build_distribution_shift_stress_validation,
    write_distribution_shift_stress_validation,
)


def test_pmb18_is_deterministic_local_and_frozen(tmp_path):
    first = json.loads(write_distribution_shift_stress_validation(tmp_path / "a").read_text())
    second = json.loads(write_distribution_shift_stress_validation(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["regime_count"] == len(SHIFT_REGIMES)
    assert first["database_access"] is False
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_retrained"] is False


def test_pmb18_covers_each_requested_distribution_shift():
    report = build_distribution_shift_stress_validation()
    names = {row["name"] for row in report["shift_regimes"]}
    assert names == {
        "control", "forecast_error_shift", "thinner_books", "wider_spreads",
        "adverse_settlement_mix", "combined_adverse_shift",
    }
    assert report["summary"]["categories"] == ["crypto", "sports", "weather"]
    assert report["summary"]["all_attribution_complete"] is True


def test_pmb18_identifies_advantage_breaks_without_threshold_tuning():
    report = build_distribution_shift_stress_validation()
    assert "advantage_break_regimes" in report["break_analysis"]
    assert all(
        "capital_and_drawdown_advantage_survived" in row["comparison"]
        for row in report["shift_regimes"]
    )
    combined = next(
        row for row in report["shift_regimes"] if row["name"] == "combined_adverse_shift"
    )
    assert combined["forecast_shift"] == "-0.08"
    assert combined["depth_multiplier"] == "0.20"
    assert combined["spread_addition"] == "0.12"
