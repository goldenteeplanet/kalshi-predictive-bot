import json
from pathlib import Path

from kalshi_predictor.benchmarking.runtime_compatibility import (
    build_runtime_field_compatibility_preview,
    write_runtime_field_compatibility_preview,
)

FIXTURES = Path(__file__).parent / "fixtures/pmb34/runtime_ranking_risk_exports.json"


def test_pmb34_generates_shadow_only_for_complete_runtime_shape():
    report = build_runtime_field_compatibility_preview(FIXTURES)
    assert report["summary"]["fixtures"] == 3
    assert report["summary"]["compatible"] == 1
    assert report["summary"]["shadow_previews_generated"] == 1
    complete = next(row for row in report["rows"] if row["fixture_id"] == "complete-current")
    assert complete["shadow_preview"]["runtime_effect"] == "NONE_DISABLED_SHADOW"


def test_pmb34_reports_exact_gaps_without_fabrication():
    report = build_runtime_field_compatibility_preview(FIXTURES)
    assert report["exact_runtime_gaps"] == ["forecast_bias", "spread_addition"]
    legacy = next(
        row for row in report["rows"] if row["fixture_id"] == "legacy-missing-shadow-context"
    )
    assert legacy["normalized"] is None
    assert legacy["shadow_preview"] is None
    assert report["summary"]["runtime_activation_ready"] is False


def test_pmb34_is_deterministic_local_and_disabled(tmp_path):
    first = json.loads(write_runtime_field_compatibility_preview(FIXTURES, tmp_path / "a").read_text())
    second = json.loads(write_runtime_field_compatibility_preview(FIXTURES, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
