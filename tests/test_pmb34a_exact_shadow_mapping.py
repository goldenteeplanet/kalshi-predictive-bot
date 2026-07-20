import json
from pathlib import Path

from kalshi_predictor.benchmarking.exact_shadow_mapping import (
    build_exact_shadow_field_mapping_preview,
    map_exact_shadow_context,
    write_exact_shadow_field_mapping_preview,
)


FIXTURES = Path(__file__).parent / "fixtures/pmb34a/exact_shadow_source_exports.json"


def test_pmb34a_maps_exact_real_sources_for_all_categories():
    report = build_exact_shadow_field_mapping_preview(FIXTURES)
    exact = {row["category"]: row for row in report["rows"] if row["compatible"]}
    assert set(exact) == {"crypto", "weather", "sports"}
    assert exact["crypto"]["shadow_context"] == {
        "forecast_bias": "0.004", "spread_addition": "0.006"
    }
    assert exact["weather"]["shadow_context"] == {
        "forecast_bias": "-0.008", "spread_addition": "0.008"
    }
    assert exact["sports"]["mapping_provenance"]["forecast_bias"]["reference_forecast_id"] == 193


def test_pmb34a_rejects_missing_or_fabricated_defaults():
    fixtures = json.loads(FIXTURES.read_text())
    missing = next(row for row in fixtures if row["fixture_id"] == "weather-reject-missing-reference")
    result = map_exact_shadow_context(missing)
    assert result["mapped"] is False
    assert result["shadow_context"] is None
    assert "REFERENCE_FORECAST_FIELD_MISSING:probability" in result["diagnostics"]
    report = build_exact_shadow_field_mapping_preview(FIXTURES)
    rejected = next(row for row in report["rows"] if row["fixture_id"] == missing["fixture_id"])
    assert rejected["shadow_preview"] is None
    assert report["default_or_fabricated_values_allowed"] is False
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_pmb34a_is_deterministic_local_and_disabled(tmp_path):
    first = json.loads(write_exact_shadow_field_mapping_preview(FIXTURES, tmp_path / "a").read_text())
    second = json.loads(write_exact_shadow_field_mapping_preview(FIXTURES, tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["required_categories_pass"] is True
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
