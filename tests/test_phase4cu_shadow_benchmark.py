from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cu_shadow_benchmark.py"
    spec = importlib.util.spec_from_file_location("phase4cu_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _components():
    return {
        "catalog": {"sequence": 1, "records": [{"key": "market", "value": 1}]},
        "quotes": {"sequence": 1, "records": [{"key": "bid", "value": 55}]},
        "weather": {"sequence": 1, "records": [{"key": "rain", "value": 1.2}]},
    }


def _fixture(module, identifier, fixture_class, changed):
    previous = _components()
    current = deepcopy(previous)
    if changed:
        current["quotes"]["sequence"] = 2
        current["quotes"]["records"][0]["value"] = 56
    return {
        "fixture_id": identifier,
        "class": fixture_class,
        "previous": previous,
        "current": current,
        "prior_attestation": module.prior_attestation(previous),
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "fixtures": [
            _fixture(module, "unchanged", "steady-state", False),
            _fixture(module, "quote-change", "single-component-change", True),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_representative_replay_is_equivalent_and_strictly_cheaper():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "SHADOW_BENCHMARK_PASS"
    assert report["strict_improvement_count"] == 2
    assert report["total_work_units_saved"] > 0
    assert all(row["outputs_equivalent"] for row in report["comparisons"])
    assert all(
        row["candidate"]["work_units"] < row["baseline"]["work_units"]
        for row in report["comparisons"]
    )
    assert report["wall_clock_used_for_verdict"] is False


def test_changed_only_validation_count_is_reported():
    module = _module()
    report = module.build_report(_payload(module))
    by_id = {row["fixture_id"]: row for row in report["comparisons"]}
    assert by_id["unchanged"]["candidate"]["validated_components"] == 0
    assert by_id["quote-change"]["candidate"]["validated_components"] == 1
    assert by_id["quote-change"]["baseline"]["validated_components"] == 3


def test_invalid_changed_component_refusal_remains_equivalent():
    module = _module()
    payload = _payload(module)
    payload["fixtures"][1]["current"]["quotes"]["records"][0]["value"] = float("inf")
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["comparisons"][1]["outputs_equivalent"] is True
    assert report["comparisons"][1]["status"] == "PASS"


@pytest.mark.parametrize("kind", ["hash", "snapshot", "components", "statuses"])
def test_invalid_prior_attestation_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    attestation = payload["fixtures"][0]["prior_attestation"]
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


@pytest.mark.parametrize("kind", ["empty", "fields", "duplicate", "class", "sets"])
def test_malformed_fixture_sets_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["fixtures"] = []
    elif kind == "fields":
        payload["fixtures"][0]["extra"] = True
    elif kind == "duplicate":
        payload["fixtures"][1]["fixture_id"] = "unchanged"
    elif kind == "class":
        payload["fixtures"][0]["class"] = ""
    else:
        del payload["fixtures"][0]["current"]["catalog"]
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["fixtures"][0]["class"] = "tampered"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "shadow.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (Path(__file__).parents[1] / "scripts/local/phase4cu_shadow_benchmark.py").read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
