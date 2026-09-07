from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bo_final_certification.py"
    spec = importlib.util.spec_from_file_location("phase4bo_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    metadata = {"resolved_path_hash": module.canonical_hash("copied identity"), "size": 100}
    payload = {
        "schema": module.INPUT_SCHEMA,
        "requirements": [
            {
                "requirement": requirement,
                "passed": True,
                "evidence_hash": module.canonical_hash(requirement),
            }
            for requirement in module.REQUIREMENTS
        ],
        "phases": list(module.PHASES),
        "production_metadata_before": metadata,
        "production_metadata_after": dict(metadata),
        "residual_risks": [{"id": "EXTERNAL_IDENTITY_TRUST", "severity": "MEDIUM"}],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def test_final_certification_has_only_success_state_and_explicit_meaning():
    module = _module()
    outputs = module.certify(_payload(module))
    certification, risks, index, report = outputs
    assert certification["terminal_state"] == module.TERMINAL
    assert report["terminal_state"] == module.TERMINAL
    assert certification["meaning"]["offline_protocol_and_safeguards_certified"] is True
    assert (
        certification["meaning"]["future_scope_expansion_requires_separate_user_authorization"]
        is True
    )
    assert certification["meaning"]["production_mutation_performed"] is False
    assert risks["high_risk_count"] == 0
    assert index["phases"] == list(module.PHASES)
    assert outputs == module.certify(_payload(module))


@pytest.mark.parametrize("index", range(len(_module().REQUIREMENTS)))
def test_every_certification_requirement_fails_closed(index: int):
    module = _module()
    payload = _payload(module)
    payload["requirements"][index]["passed"] = False
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="REQUIREMENT_FAILED"):
        module.certify(payload)


def test_missing_reordered_lineage_metadata_high_risk_and_tamper_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["requirements"].pop()
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="COVERAGE"):
        module.certify(payload)
    payload = _payload(module)
    payload["phases"].pop()
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="LINEAGE"):
        module.certify(payload)
    payload = _payload(module)
    payload["production_metadata_after"]["size"] += 1
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="PRODUCTION_IDENTITY"):
        module.certify(payload)
    payload = _payload(module)
    payload["residual_risks"].append({"id": "bad", "severity": "HIGH"})
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="RESIDUAL_RISK"):
        module.certify(payload)
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.certify(payload)


def test_source_has_no_production_execution_capability():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bo_final_certification.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "systemctl",
        "requests",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source


def test_atomic_publication_replaces_complete_json(tmp_path: Path):
    module = _module()
    output = tmp_path / "nested" / "certification.json"
    certification = module.certify(_payload(module))[0]
    module.atomic_write_json(output, certification)
    import json

    assert json.loads(output.read_text()) == certification
    assert list(output.parent.glob(f".{output.name}.*")) == []
