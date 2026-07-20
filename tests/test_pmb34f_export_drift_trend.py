import copy
import json
from pathlib import Path

from kalshi_predictor.benchmarking.export_drift_trend import (
    analyze_dataset_trend,
    build_export_drift_trend_preview,
    write_export_drift_trend_preview,
)
from kalshi_predictor.benchmarking.runtime_export_import import import_runtime_export_manifest


FIXTURES = Path(__file__).parent / "fixtures"


def _datasets():
    result = import_runtime_export_manifest(FIXTURES / "pmb34c/manifest.json")
    assert result["valid"]
    return result["datasets"]


def test_pmb34f_certifies_three_ordered_custody_bundles():
    report = build_export_drift_trend_preview(FIXTURES / "pmb34f/trend_manifest.json")
    assert report["summary"]["certification_passed"] is True
    assert len(report["custody_bundles"]) == 3
    assert report["trend"]["summary"]["transitions"] == 2
    assert report["trend"]["summary"]["alerts"] == 0
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_pmb34f_marks_recurring_declared_model_drift_medium():
    first = _datasets()
    second = copy.deepcopy(first)
    third = copy.deepcopy(second)
    second["forecasts"][0]["probability"] = "0.610"
    third["forecasts"][0]["probability"] = "0.620"
    change = "forecasts:501:probability"
    result = analyze_dataset_trend([first, second, third], [{change}, {change}])
    assert result["certified"] is True
    assert result["recurring_drift"] == [{"signature": "forecasts:probability", "occurrences": 2}]
    assert result["summary"]["medium"] == 2
    assert all(alert["reason"] == "RECURRING_FIELD_DRIFT" for alert in result["alerts"])


def test_pmb34f_alerts_on_schema_and_provenance_breaks():
    first = _datasets()
    second = copy.deepcopy(first)
    second["rankings"][0]["new_field"] = "x"
    third = copy.deepcopy(second)
    third["rankings"][0]["market_snapshot_id"] = 999
    result = analyze_dataset_trend([first, second, third])
    assert result["certified"] is False
    assert result["summary"]["high"] == 1
    assert result["summary"]["critical"] == 1
    assert result["alerts"][0]["severity"] == "CRITICAL"


def test_pmb34f_rejects_bounds_and_declaration_count():
    snapshot = _datasets()
    too_short = analyze_dataset_trend([snapshot, snapshot])
    assert too_short["certified"] is False
    assert too_short["diagnostics"][0].startswith("BUNDLE_COUNT_OUT_OF_RANGE")
    mismatch = analyze_dataset_trend([snapshot, snapshot, snapshot], [set()])
    assert mismatch["diagnostics"] == ["DECLARATION_TRANSITION_COUNT_MISMATCH"]


def test_pmb34f_rejects_duplicate_or_unordered_bundle_timestamps(tmp_path):
    source = json.loads((FIXTURES / "pmb34f/trend_manifest.json").read_text())
    source["bundles"][1]["observed_at"] = source["bundles"][0]["observed_at"]
    path = tmp_path / "manifest.json"
    # Keep custody references valid from the temporary manifest root.
    source["bundles"] = [
        {**bundle, "custody": str((FIXTURES / "pmb34c/custody_manifest.json").resolve())}
        for bundle in source["bundles"]
    ]
    path.write_text(json.dumps(source))
    report = build_export_drift_trend_preview(path)
    assert any(code.startswith("DUPLICATE_BUNDLE_TIMESTAMP") for code in report["series_diagnostics"])


def test_pmb34f_is_deterministic_local_and_disabled(tmp_path):
    source = FIXTURES / "pmb34f/trend_manifest.json"
    first = json.loads(write_export_drift_trend_preview(source, tmp_path / "a").read_text())
    second = json.loads(write_export_drift_trend_preview(source, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
