from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bm_airgap_acceptance.py"
    spec = importlib.util.spec_from_file_location("phase4bm_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "checks": [
            {"check": check, "passed": True, "evidence_hash": module.canonical_hash(check)}
            for check in module.CHECKS
        ],
        "refusal_classes": ["ARTIFACT_TAMPERED", "EXPIRED", "LINEAGE_INVALID", "WRONG_DATABASE"],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def test_airgap_acceptance_is_complete_deterministic_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.evaluate(payload) == module.evaluate(payload)
    report = module.evaluate(payload)
    assert report["accepted"] is True
    assert report["network_attempts"] == report["production_dependencies"] == 0
    assert report["execution_authorized"] is False


@pytest.mark.parametrize("index", range(len(_module().CHECKS)))
def test_every_required_check_fails_closed(index: int):
    module = _module()
    payload = _payload(module)
    payload["checks"][index]["passed"] = False
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="CHECK_FAILED"):
        module.evaluate(payload)


def test_missing_reordered_duplicate_refusals_tampering_and_bad_hash_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["checks"].pop()
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="COVERAGE"):
        module.evaluate(payload)
    payload = _payload(module)
    payload["checks"].reverse()
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="ORDER"):
        module.evaluate(payload)
    payload = _payload(module)
    payload["refusal_classes"].append(payload["refusal_classes"][0])
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="REFUSAL_COVERAGE"):
        module.evaluate(payload)
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.evaluate(payload)
    payload = _payload(module)
    payload["checks"][0]["evidence_hash"] = "bad"
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="CHECK_FAILED"):
        module.evaluate(payload)


def test_source_has_no_network_service_or_production_runtime_surface():
    source = (Path(__file__).parents[1] / "scripts/local/phase4bm_airgap_acceptance.py").read_text()
    for token in ("requests", "urllib", "socket", "systemctl", "/home/james", "sqlite3"):
        assert token not in source


def test_atomic_publication_replaces_complete_json(tmp_path: Path):
    module = _module()
    output = tmp_path / "nested" / "report.json"
    report = module.evaluate(_payload(module))
    module.atomic_write_json(output, report)
    import json

    assert json.loads(output.read_text()) == report
    assert list(output.parent.glob(f".{output.name}.*")) == []
