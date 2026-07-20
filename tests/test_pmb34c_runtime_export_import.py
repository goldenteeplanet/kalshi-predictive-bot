import json
import shutil
from pathlib import Path

from kalshi_predictor.benchmarking.runtime_export_import import (
    build_runtime_export_import_preview,
    import_runtime_export_manifest,
    write_runtime_export_import_preview,
)


FIXTURES = Path(__file__).parent / "fixtures/pmb34c"


def _copy_fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "exports"
    shutil.copytree(FIXTURES, target)
    return target / "manifest.json"


def test_pmb34c_imports_mixed_json_csv_and_certifies_all_categories():
    report = build_runtime_export_import_preview(FIXTURES / "manifest.json")
    assert report["manifest_valid"] is True
    assert report["summary"]["decisions"] == 3
    assert report["summary"]["certified"] == 3
    assert report["summary"]["category_coverage"] == ["crypto", "sports", "weather"]
    assert all(row["shadow_preview"]["runtime_effect"] == "NONE_DISABLED_SHADOW" for row in report["rows"])
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_pmb34c_rejects_malformed_schema_and_csv_types(tmp_path):
    manifest = _copy_fixtures(tmp_path)
    decisions_path = manifest.parent / "decisions.json"
    decisions = json.loads(decisions_path.read_text())
    del decisions[0]["target_time"]
    decisions_path.write_text(json.dumps(decisions))
    imported = import_runtime_export_manifest(manifest)
    assert imported["valid"] is False
    assert "SCHEMA_FIELD_MISSING:decisions:0:target_time" in imported["diagnostics"]

    manifest = _copy_fixtures(tmp_path / "second")
    risks_path = manifest.parent / "risks.json"
    payload = json.loads(manifest.read_text())
    payload["datasets"]["risks"] = {"format": "csv", "path": "risks.csv"}
    manifest.write_text(json.dumps(payload))
    risks_path.unlink()
    (manifest.parent / "risks.csv").write_text("ticker,risk_gate_passed,requested_capital\nPMB34C-CRYPTO,maybe,10\n")
    imported = import_runtime_export_manifest(manifest)
    assert any(code.startswith("DATASET_READ_ERROR:risks:ValueError") for code in imported["diagnostics"])


def test_pmb34c_rejects_duplicate_associations_and_path_escape(tmp_path):
    manifest = _copy_fixtures(tmp_path)
    rankings_path = manifest.parent / "rankings.json"
    rankings = json.loads(rankings_path.read_text())
    rankings.append(dict(rankings[0]))
    rankings_path.write_text(json.dumps(rankings))
    report = build_runtime_export_import_preview(manifest)
    crypto = next(row for row in report["rows"] if row["ticker"] == "PMB34C-CRYPTO")
    assert "ASSOCIATION_AMBIGUOUS:rankings:PMB34C-CRYPTO" in crypto["diagnostics"]

    payload = json.loads(manifest.read_text())
    payload["datasets"]["books"]["path"] = "../outside.csv"
    manifest.write_text(json.dumps(payload))
    imported = import_runtime_export_manifest(manifest)
    assert "DATASET_PATH_OUTSIDE_MANIFEST_ROOT:books" in imported["diagnostics"]


def test_pmb34c_is_deterministic_local_and_disabled(tmp_path):
    first = json.loads(write_runtime_export_import_preview(FIXTURES / "manifest.json", tmp_path / "a").read_text())
    second = json.loads(write_runtime_export_import_preview(FIXTURES / "manifest.json", tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
