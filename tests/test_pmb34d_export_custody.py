import json
import shutil
from pathlib import Path

from kalshi_predictor.benchmarking.export_custody import (
    build_export_custody_preview,
    certify_export_custody,
    write_export_custody_preview,
)


FIXTURES = Path(__file__).parent / "fixtures/pmb34c"


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "custody"
    shutil.copytree(FIXTURES, target)
    return target / "custody_manifest.json"


def test_pmb34d_certifies_hashes_metadata_redaction_and_import():
    report = build_export_custody_preview(FIXTURES / "custody_manifest.json")
    assert report["custody_certified"] is True
    assert report["summary"]["import_certified"] is True
    assert report["summary"]["certification_passed"] is True
    assert report["summary"]["artifacts"] == 5
    assert report["summary"]["sensitive_fields_rejected"] == 0
    assert report["signing_metadata"]["attestation_type"] == "offline-sha256-manifest-attestation-v1"
    assert len(report["chain_digest"]) == 64
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_pmb34d_rejects_tampering_and_attestation_changes(tmp_path):
    custody = _copy(tmp_path)
    forecasts = custody.parent / "forecasts.csv"
    forecasts.write_text(forecasts.read_text() + "\n", encoding="utf-8")
    result = certify_export_custody(custody)
    assert result["certified"] is False
    assert "ARTIFACT_HASH_MISMATCH:forecasts" in result["diagnostics"]

    custody = _copy(tmp_path / "second")
    payload = json.loads(custody.read_text())
    payload["signer_id"] = "changed-after-attestation"
    custody.write_text(json.dumps(payload), encoding="utf-8")
    result = certify_export_custody(custody)
    assert "ATTESTATION_DIGEST_MISMATCH" in result["diagnostics"]


def test_pmb34d_rejects_sensitive_json_and_csv_fields(tmp_path):
    custody = _copy(tmp_path)
    decisions = custody.parent / "decisions.json"
    payload = json.loads(decisions.read_text())
    payload[0]["api_key"] = "must-not-pass"
    decisions.write_text(json.dumps(payload), encoding="utf-8")
    result = certify_export_custody(custody)
    assert any(code.startswith("SENSITIVE_FIELD_REJECTED:decisions:") for code in result["diagnostics"])

    custody = _copy(tmp_path / "second")
    books = custody.parent / "books.csv"
    lines = books.read_text().splitlines()
    lines[0] += ",session_token"
    lines[1:] = [line + ",redacted" for line in lines[1:]]
    books.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = certify_export_custody(custody)
    assert "SENSITIVE_FIELD_REJECTED:books:$.columns.session_token" in result["diagnostics"]


def test_pmb34d_is_deterministic_local_and_disabled(tmp_path):
    source = FIXTURES / "custody_manifest.json"
    first = json.loads(write_export_custody_preview(source, tmp_path / "a").read_text())
    second = json.loads(write_export_custody_preview(source, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
