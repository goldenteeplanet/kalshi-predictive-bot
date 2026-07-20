import copy
import json
from pathlib import Path

from kalshi_predictor.benchmarking.export_drift import (
    build_export_drift_preview,
    compare_export_datasets,
    write_export_drift_preview,
)
from kalshi_predictor.benchmarking.runtime_export_import import import_runtime_export_manifest


FIXTURES = Path(__file__).parent / "fixtures"


def _datasets():
    imported = import_runtime_export_manifest(FIXTURES / "pmb34c/manifest.json")
    assert imported["valid"]
    return imported["datasets"]


def test_pmb34e_certifies_identical_custody_bundles():
    report = build_export_drift_preview(FIXTURES / "pmb34e/comparison.json")
    assert report["baseline_custody_certified"] is True
    assert report["candidate_custody_certified"] is True
    assert report["drift"]["summary"]["changes"] == 0
    assert report["summary"]["certification_passed"] is True
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_pmb34e_attributes_declared_forecast_snapshot_ranking_and_risk_changes():
    baseline = _datasets()
    candidate = copy.deepcopy(baseline)
    candidate["forecasts"][0]["probability"] = "0.610"
    candidate["books"][0]["executable_spread"] = "0.030"
    candidate["rankings"][0]["opportunity_score"] = "80"
    candidate["risks"][0]["requested_capital"] = "9"
    declared = {
        "forecasts:501:probability",
        "books:601:executable_spread",
        "rankings:PMB34C-CRYPTO|crypto:opportunity_score",
        "risks:PMB34C-CRYPTO:requested_capital",
    }
    result = compare_export_datasets(baseline, candidate, declared)
    assert result["certified"] is True
    assert result["summary"]["changes"] == 4
    assert result["summary"]["explained"] == 4
    assert result["summary"]["provenance_breaking"] == 0


def test_pmb34e_rejects_unexplained_provenance_and_schema_drift():
    baseline = _datasets()
    candidate = copy.deepcopy(baseline)
    candidate["rankings"][0]["market_snapshot_id"] = 999
    candidate["rankings"][0]["new_runtime_field"] = "unexpected"
    result = compare_export_datasets(baseline, candidate)
    assert result["certified"] is False
    assert "PROVENANCE_BREAKING_DRIFT:rankings:PMB34C-CRYPTO|crypto:market_snapshot_id" in result["diagnostics"]
    assert "UNEXPLAINED_DRIFT:rankings:PMB34C-CRYPTO|crypto:new_runtime_field" in result["diagnostics"]
    assert result["summary"]["schema_changes"] == 1


def test_pmb34e_rejects_removed_rows_and_unused_declarations():
    baseline = _datasets()
    candidate = copy.deepcopy(baseline)
    candidate["books"] = candidate["books"][1:]
    result = compare_export_datasets(
        baseline, candidate, {"forecasts:501:probability"}
    )
    assert result["certified"] is False
    assert any(code.startswith("PROVENANCE_BREAKING_DRIFT:books:601:ROW_REMOVED") for code in result["diagnostics"])
    assert "DECLARED_CHANGE_NOT_OBSERVED:forecasts:501:probability" in result["diagnostics"]


def test_pmb34e_is_deterministic_local_and_disabled(tmp_path):
    source = FIXTURES / "pmb34e/comparison.json"
    first = json.loads(write_export_drift_preview(source, tmp_path / "a").read_text())
    second = json.loads(write_export_drift_preview(source, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
