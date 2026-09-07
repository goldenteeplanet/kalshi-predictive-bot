from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cq_incremental_coherence.py"
    spec = importlib.util.spec_from_file_location("phase4cq_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _components():
    return {
        "catalog": {
            "sequence": 1,
            "captured_at": "2026-08-26T00:00:00Z",
            "records": [{"key": "market", "value": 1}],
        },
        "quotes": {
            "sequence": 2,
            "captured_at": "2026-08-26T00:00:00.050000Z",
            "records": [{"key": "bid", "value": 55}],
        },
    }


def _payload(module, cycles=0):
    previous = _components()
    current = deepcopy(previous)
    current["quotes"]["sequence"] = 3
    current["quotes"]["records"][0]["value"] = 56
    payload = {
        "schema": module.INPUT_SCHEMA,
        "full_validation_interval": 3,
        "cycles_since_full": cycles,
        "max_component_skew_ms": 100,
        "previous_components": previous,
        "current_components": current,
        "prior_full_attestation": module.build_attestation(previous),
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_only_changed_component_is_incrementally_validated_with_equivalence_proof():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["validation_scope"] == "INCREMENTAL"
    assert report["changed_components"] == ["quotes"]
    assert report["validated_components"] == ["quotes"]
    assert report["carried_components"] == ["catalog"]
    assert report["snapshot_verdict"] == "PASS"
    assert report["equivalence_proven"] is True
    assert report["runtime_validator_changed"] is False


def test_exact_interval_boundary_forces_full_validation():
    module = _module()
    report = module.build_report(_payload(module, cycles=2))
    assert report["validation_scope"] == "FULL"
    assert report["validated_components"] == ["catalog", "quotes"]
    assert report["carried_components"] == []


def test_unchanged_snapshot_validates_nothing_before_full_boundary():
    module = _module()
    payload = _payload(module)
    payload["current_components"] = deepcopy(payload["previous_components"])
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["changed_components"] == []
    assert report["validated_components"] == []
    assert report["equivalence_proven"] is True


@pytest.mark.parametrize("kind", ["shape", "timestamp", "sequence", "records", "key", "value"])
def test_changed_component_validation_failures_are_deterministic(kind: str):
    module = _module()
    payload = _payload(module)
    component = payload["current_components"]["quotes"]
    if kind == "shape":
        component["extra"] = True
    elif kind == "timestamp":
        component["captured_at"] = "bad"
    elif kind == "sequence":
        component["sequence"] = True
    elif kind == "records":
        component["records"] = "bad"
    elif kind == "key":
        component["records"].append({"key": "bid", "value": 1})
    else:
        component["records"][0]["value"] = float("inf")
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["component_statuses"]["quotes"] != "PASS"
    assert report["snapshot_verdict"] == "INVALID_COMPONENT"


def test_temporal_incoherence_is_detected_globally():
    module = _module()
    payload = _payload(module)
    payload["current_components"]["quotes"]["captured_at"] = "2026-08-26T00:00:00.101000Z"
    _rehash(module, payload)
    assert module.build_report(payload)["snapshot_verdict"] == "INCOHERENT_COMPONENT_TIME"


@pytest.mark.parametrize("kind", ["hash", "snapshot", "components", "statuses"])
def test_invalid_prior_attestation_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    attestation = payload["prior_full_attestation"]
    if kind == "hash":
        attestation["artifact_hash"] = "0" * 64
    elif kind == "snapshot":
        attestation["snapshot_hash"] = "1" * 64
        attestation["artifact_hash"] = module._hash(attestation)
    elif kind == "components":
        attestation["component_hashes"]["catalog"] = "2" * 64
        attestation["artifact_hash"] = module._hash(attestation)
    else:
        del attestation["component_statuses"]["catalog"]
        attestation["artifact_hash"] = module._hash(attestation)
    _rehash(module, payload)
    with pytest.raises(ValueError, match="ATTESTATION"):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["interval", "cycles", "skew", "sets", "empty"])
def test_invalid_policy_or_component_sets_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "interval":
        payload["full_validation_interval"] = 0
    elif kind == "cycles":
        payload["cycles_since_full"] = 3
    elif kind == "skew":
        payload["max_component_skew_ms"] = 0
    elif kind == "sets":
        del payload["current_components"]["catalog"]
    else:
        payload["previous_components"] = {}
        payload["current_components"] = {}
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_outer_tampering_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["current_components"]["quotes"]["sequence"] = 4
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    output = tmp_path / "incremental.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cq_incremental_coherence.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
