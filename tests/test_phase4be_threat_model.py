from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4be_threat_model.py"
    spec = importlib.util.spec_from_file_location("phase4be_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    threats = [
        {
            "threat": threat,
            "severity": "HIGH",
            "controls": sorted(module.REQUIRED_CONTROLS[threat]),
            "evidence_hashes": [module.canonical_hash({"threat": threat, "test": "PASS"})],
            "adversarial_test_outcome": "PASS",
            "residual_risk": "LOW",
        }
        for threat in module.THREATS
    ]
    payload = {"schema": module.INPUT_SCHEMA, "threats": threats}
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "threats.json"
    path.write_text(json.dumps(payload))
    return module, path


def _mutate(module, path: Path, mutate, *, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_complete_formal_threat_model_passes_all_adversarial_evidence(tmp_path: Path):
    module, path = _fixture(tmp_path)
    model, risk = module.build(path, now=NOW)
    assert model["threat_count"] == len(module.THREATS) == 14
    assert model["complete_threat_coverage"] is True
    assert model["all_required_mitigations_verified"] is True
    assert risk["advancement_allowed"] is True
    assert risk["unresolved_high_severity_count"] == 0
    assert model["artifact_hash"] == module._hash(model)


@pytest.mark.parametrize("threat", _module().THREATS)
def test_every_threat_fails_when_one_required_control_is_removed(tmp_path: Path, threat: str):
    module, path = _fixture(tmp_path)

    def mutate(payload):
        row = next(item for item in payload["threats"] if item["threat"] == threat)
        row["controls"].pop()

    _mutate(module, path, mutate)
    model, risk = module.build(path, now=NOW)
    row = next(item for item in model["rows"] if item["threat"] == threat)
    assert row["mitigated"] is False
    assert any(reason.startswith("MISSING_CONTROL:") for reason in row["reason_codes"])
    assert risk["advancement_allowed"] is False


def test_failed_adversarial_test_and_high_residual_block_advancement(tmp_path: Path):
    module, path = _fixture(tmp_path)
    _mutate(
        module,
        path,
        lambda p: p["threats"][0].update(adversarial_test_outcome="FAIL", residual_risk="HIGH"),
    )
    _, risk = module.build(path, now=NOW)
    reasons = {finding["reason"] for finding in risk["findings"]}
    assert {"ADVERSARIAL_TEST_NOT_PASSING", "UNRESOLVED_HIGH_SEVERITY_RISK"} <= reasons
    assert risk["unresolved_high_severity_count"] == 1


def test_missing_duplicate_reordered_and_unknown_threats_fail_closed(tmp_path: Path):
    module, path = _fixture(tmp_path / "missing")
    _mutate(module, path, lambda p: p["threats"].pop())
    with pytest.raises(ValueError, match="THREAT_COVERAGE_OR_ORDER_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "reorder")
    _mutate(module, path, lambda p: p["threats"].reverse())
    with pytest.raises(ValueError, match="THREAT_COVERAGE_OR_ORDER_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "unknown")
    _mutate(module, path, lambda p: p["threats"][0].update(threat="UNKNOWN"))
    with pytest.raises(ValueError, match="THREAT_COVERAGE_OR_ORDER_INVALID"):
        module.build(path, now=NOW)


def test_evidence_hash_control_duplication_risk_levels_tampering_and_time_fail_closed(
    tmp_path: Path,
):
    module, path = _fixture(tmp_path / "hash")
    _mutate(module, path, lambda p: p["threats"][0].update(evidence_hashes=["short"]))
    with pytest.raises(ValueError, match="EVIDENCE_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "control")
    _mutate(
        module,
        path,
        lambda p: p["threats"][0].update(controls=["CANONICAL_HASH", "CANONICAL_HASH"]),
    )
    with pytest.raises(ValueError, match="CONTROLS_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "risk")
    _mutate(module, path, lambda p: p["threats"][0].update(residual_risk="CRITICAL"))
    with pytest.raises(ValueError, match="RISK_LEVEL_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "tamper")
    _mutate(module, path, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="INPUT_SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))


def test_static_model_is_artifact_only_and_nonexecuting():
    source = (Path(__file__).parents[1] / "scripts/local/phase4be_threat_model.py").read_text()
    assert "sqlite3" not in source
    assert "subprocess" not in source
    assert "systemctl" not in source
    assert "--production-db" not in source
