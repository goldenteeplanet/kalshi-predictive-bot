from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bz_workstream_i_certification.py"
    spec = importlib.util.spec_from_file_location("phase4bz_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(module, schema):
    artifact = {"schema": schema, "execution_authorized": False}
    if schema == "phase4bw.read-only-profile.v1":
        artifact["all_envelopes_satisfied"] = True
    if schema == "phase4by.optimization-equivalence-proof.v1":
        artifact.update(all_outputs_byte_identical=True, refusal_order_changed=False)
    artifact["artifact_hash"] = module._hash(artifact)
    return artifact


def _bundle(module, *, regression=False):
    evidence = [_artifact(module, schema) for schema in module.EVIDENCE_SCHEMAS]
    benchmarks = [
        {
            "name": name,
            "before_elapsed_ns": 100 + index,
            "after_elapsed_ns": (101 + index if regression and index == 0 else 90 + index),
            "output_hash": f"{index:064x}",
        }
        for index, name in enumerate(module.BENCHMARK_NAMES)
    ]
    bundle = {
        "schema": module.INPUT_SCHEMA,
        "evidence": evidence,
        "benchmarks": benchmarks,
        "residual_uncertainty": [
            "synthetic workloads do not reproduce production scheduler contention",
            "local filesystem timings vary by host",
        ],
    }
    bundle["artifact_hash"] = module._hash(bundle)
    return bundle


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_certification_is_deterministic_complete_and_non_authorizing():
    module = _module()
    report = module.build_certification(_bundle(module))
    assert report == module.build_certification(_bundle(module))
    assert report["certification_state"] == "WORKSTREAM_I_CERTIFIED"
    assert report["certified"] is True
    assert len(report["evidence_hashes"]) == len(module.EVIDENCE_SCHEMAS)
    assert report["production_records_created"] == 0
    assert report["execution_authorized"] is False


def test_benchmark_regression_is_visible_and_not_certified():
    module = _module()
    report = module.build_certification(_bundle(module, regression=True))
    assert report["certified"] is False
    assert report["benchmarks"][0]["non_regressing"] is False
    assert report["certification_state"] == "WORKSTREAM_I_BENCHMARK_REGRESSION"


@pytest.mark.parametrize(
    "kind",
    ("outer", "evidence_order", "evidence_hash", "authority", "profile", "proof", "benchmarks"),
)
def test_invalid_or_incomplete_evidence_fails_closed(kind: str):
    module = _module()
    bundle = _bundle(module)
    if kind == "outer":
        bundle["extra"] = True
    elif kind == "evidence_order":
        bundle["evidence"].reverse()
        _rehash(module, bundle)
    elif kind == "evidence_hash":
        bundle["evidence"][0]["artifact_hash"] = "0" * 64
        _rehash(module, bundle)
    elif kind == "authority":
        bundle["evidence"][0]["execution_authorized"] = True
        _rehash(module, bundle["evidence"][0])
        _rehash(module, bundle)
    elif kind == "profile":
        bundle["evidence"][3]["all_envelopes_satisfied"] = False
        _rehash(module, bundle["evidence"][3])
        _rehash(module, bundle)
    elif kind == "proof":
        bundle["evidence"][5]["all_outputs_byte_identical"] = False
        _rehash(module, bundle["evidence"][5])
        _rehash(module, bundle)
    else:
        bundle["benchmarks"].pop()
        _rehash(module, bundle)
    with pytest.raises(ValueError):
        module.build_certification(bundle)


@pytest.mark.parametrize("value", (-1, True, "1"))
def test_invalid_benchmark_values_fail_closed(value):
    module = _module()
    bundle = _bundle(module)
    bundle["benchmarks"][0]["after_elapsed_ns"] = value
    _rehash(module, bundle)
    with pytest.raises(ValueError, match="BENCHMARK_VALUE"):
        module.build_certification(bundle)


def test_missing_or_blank_uncertainty_fails_closed():
    module = _module()
    for uncertainty in ([], [""]):
        bundle = _bundle(module)
        bundle["residual_uncertainty"] = uncertainty
        _rehash(module, bundle)
        with pytest.raises(ValueError, match="UNCERTAINTY"):
            module.build_certification(bundle)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_certification(_bundle(module))
    output = tmp_path / "certification.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bz_workstream_i_certification.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
