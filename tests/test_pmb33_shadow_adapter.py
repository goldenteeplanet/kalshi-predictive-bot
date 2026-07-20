import json

from kalshi_predictor.benchmarking.shadow_adapter import (
    ExposureGuardShadowAdapter,
    SYNTHETIC_RANKINGS,
    build_exposure_guard_shadow_adapter_preview,
    write_exposure_guard_shadow_adapter_preview,
)


def test_pmb33_adapter_never_mutates_source_or_runtime():
    source = dict(SYNTHETIC_RANKINGS[0])
    original = json.loads(json.dumps(source))
    result = ExposureGuardShadowAdapter().preview(source)
    assert source == original
    assert result["source_unchanged"] is True
    assert result["runtime_effect"] == "NONE_DISABLED_SHADOW"
    assert result["policy_enabled"] is False


def test_pmb33_applies_buffer_and_scale_only_to_shadow_rows():
    report = build_exposure_guard_shadow_adapter_preview()
    assert report["policy"] == {
        "max_forecast_bias_magnitude": "0.008",
        "max_spread_addition": "0.008",
        "position_scale": "0.95",
    }
    assert report["summary"]["shadow_eligible"] < report["summary"]["baseline_eligible"]
    assert report["summary"]["buffer_rejections"] > 0
    assert all(
        row["shadow"]["allocated_capital"] == "9.50"
        for row in report["rows"] if row["shadow"]["eligible"]
    )


def test_pmb33_report_is_deterministic_complete_and_disabled(tmp_path):
    first = json.loads(write_exposure_guard_shadow_adapter_preview(tmp_path / "a").read_text())
    second = json.loads(write_exposure_guard_shadow_adapter_preview(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["all_sources_unchanged"] is True
    assert first["summary"]["all_attribution_complete"] is True
    assert first["policy_enabled"] is False
    assert first["execution_enabled"] is False
    assert first["database_writes"] == 0
