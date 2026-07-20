import json
from datetime import UTC, datetime

from kalshi_predictor.provenance.bundle import write_offline_certification_bundle
from kalshi_predictor.provenance.ci_gate import (
    build_offline_ci_gate,
    write_offline_ci_gate,
)

NOW = datetime(2026, 7, 17, 23, 0, tzinfo=UTC)


def test_prov15g_passes_reproducible_bundle_with_runtime_not_ready(tmp_path):
    bundle, manifest, _ = _bundle(tmp_path)
    first = build_offline_ci_gate(bundle_path=bundle, manifest_path=manifest, root=tmp_path)
    second = build_offline_ci_gate(bundle_path=bundle, manifest_path=manifest, root=tmp_path)
    assert first == second
    assert first["passed"] is True
    assert first["exit_code"] == 0
    assert first["runtime_attribution_release_ready"] is False
    assert first["summary"]["artifact_drift_detected"] is False


def test_prov15g_detects_artifact_and_manifest_drift(tmp_path):
    bundle, manifest, artifacts = _bundle(tmp_path)
    artifacts["PROV-15B"].write_text("{}", encoding="utf-8")
    report = build_offline_ci_gate(bundle_path=bundle, manifest_path=manifest, root=tmp_path)
    assert report["passed"] is False
    assert report["exit_code"] == 1
    assert report["summary"]["artifact_drift_detected"] is True
    artifacts["PROV-15B"].write_text('{"phase":"PROV-15B"}', encoding="utf-8")
    manifest.write_text("bad manifest\n", encoding="utf-8")
    report = build_offline_ci_gate(bundle_path=bundle, manifest_path=manifest, root=tmp_path)
    assert report["summary"]["manifest_drift_detected"] is True


def test_prov15g_writes_ci_summary_and_returns_exit_code(tmp_path):
    bundle, manifest, _ = _bundle(tmp_path)
    output, exit_code = write_offline_ci_gate(
        bundle_path=bundle, manifest_path=manifest, root=tmp_path,
        output_path=tmp_path / "ci" / "summary.json",
    )
    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert not output.with_suffix(".json.tmp").exists()


def _bundle(root):
    payloads = {
        "PROV-15": {"phase": "PROV-15", "database_access": False, "execution_enabled": False},
        "PROV-15B": {
            "phase": "PROV-15B", "database_access": False, "execution_enabled": False,
            "summary": {"passed": False, "events_failed": 2},
        },
        "PROV-15C": {
            "phase": "PROV-15C", "database_access": False, "execution_enabled": False,
            "summary": {"compatible": 3},
        },
        "PROV-15D": {
            "phase": "PROV-15D", "database_access": False, "execution_enabled": False,
            "summary": {"failed_rows": 2},
        },
        "PROV-15E": {
            "phase": "PROV-15E", "database_access": False, "execution_enabled": False,
            "certification": {"before_passed": False, "after_passed": True},
        },
    }
    artifacts = {}
    for phase, payload in payloads.items():
        path = root / f"{phase}.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        artifacts[phase] = path
    bundle, manifest = write_offline_certification_bundle(
        artifacts, generated_at=NOW, root=root, output_dir=root / "bundle"
    )
    return bundle, manifest, artifacts
