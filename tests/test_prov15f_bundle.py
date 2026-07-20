import copy
import json
from datetime import UTC, datetime

import pytest

from kalshi_predictor.provenance.bundle import (
    build_offline_certification_bundle,
    verify_offline_certification_bundle,
    write_offline_certification_bundle,
)

NOW = datetime(2026, 7, 17, 23, 0, tzinfo=UTC)


def test_prov15f_builds_and_verifies_deterministic_bundle(tmp_path):
    artifacts = _artifacts(tmp_path)
    before = {phase: path.read_bytes() for phase, path in artifacts.items()}
    first = build_offline_certification_bundle(artifacts, generated_at=NOW, root=tmp_path)
    second = build_offline_certification_bundle(artifacts, generated_at=NOW, root=tmp_path)
    assert first == second
    assert first["summary"] == {
        "artifact_count": 5, "cross_report_checks": 5,
        "cross_report_checks_passed": 5, "tooling_bundle_valid": True,
        "runtime_attribution_release_ready": False,
    }
    bundle, manifest = write_offline_certification_bundle(
        artifacts, generated_at=NOW, root=tmp_path, output_dir=tmp_path / "bundle"
    )
    assert verify_offline_certification_bundle(bundle, root=tmp_path)["verified"] is True
    assert len(manifest.read_text(encoding="utf-8").splitlines()) == 5
    assert before == {phase: path.read_bytes() for phase, path in artifacts.items()}


def test_prov15f_detects_tampered_artifact(tmp_path):
    artifacts = _artifacts(tmp_path)
    bundle, _ = write_offline_certification_bundle(
        artifacts, generated_at=NOW, root=tmp_path, output_dir=tmp_path / "bundle"
    )
    artifacts["PROV-15B"].write_text("{}", encoding="utf-8")
    result = verify_offline_certification_bundle(bundle, root=tmp_path)
    assert result["verified"] is False
    assert result["failures"] == [{"phase": "PROV-15B", "failure": "SHA256_MISMATCH"}]


def test_prov15f_rejects_missing_or_mislabeled_phase(tmp_path):
    artifacts = _artifacts(tmp_path)
    incomplete = copy.copy(artifacts)
    incomplete.pop("PROV-15E")
    with pytest.raises(ValueError, match="artifact phase mismatch"):
        build_offline_certification_bundle(incomplete, generated_at=NOW, root=tmp_path)
    artifacts["PROV-15"].write_text('{"phase":"WRONG"}', encoding="utf-8")
    with pytest.raises(ValueError, match="artifact phase mismatch for PROV-15"):
        build_offline_certification_bundle(artifacts, generated_at=NOW, root=tmp_path)


def _artifacts(root):
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
    result = {}
    for phase, payload in payloads.items():
        path = root / f"{phase}.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        result[phase] = path
    return result
