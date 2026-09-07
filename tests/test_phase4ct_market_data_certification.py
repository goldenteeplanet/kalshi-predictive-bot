from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ct_market_data_certification.py"
    spec = importlib.util.spec_from_file_location("phase4ct_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    rows = []
    for index, phase in enumerate(module.REQUIRED_PHASES):
        row = {
            "phase": phase,
            "focused_tests_passed": True,
            "cumulative_tests_passed": True,
            "latency_before_us": 100,
            "latency_after_us": 90 if index == 0 else 100,
            "freshness": "PASS",
            "coherence": "PASS",
            "provenance": "PASS",
            "safety": "PASS",
        }
        row["evidence_hash"] = module._hash(row)
        rows.append(row)
    payload = {"schema": module.INPUT_SCHEMA, "phase_evidence": rows}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash_row_and_payload(module, payload, index):
    payload["phase_evidence"][index]["evidence_hash"] = module._hash(
        payload["phase_evidence"][index]
    )
    payload["artifact_hash"] = module._hash(payload)


def test_complete_nonregressing_evidence_with_strict_improvement_certifies():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "MARKET_DATA_WORKSTREAM_CERTIFIED"
    assert report["certified"] is True
    assert report["strict_improvement_count"] == 1
    assert report["total_latency_reduction_us"] == 10
    assert len(report["phase_decisions"]) == 19
    assert report["execution_authorized"] is False


@pytest.mark.parametrize(
    "kind", ["focused", "cumulative", "freshness", "coherence", "provenance", "safety"]
)
def test_failed_test_or_invariant_gate_refuses(kind: str):
    module = _module()
    payload = _payload(module)
    if kind in {"focused", "cumulative"}:
        payload["phase_evidence"][0][f"{kind}_tests_passed"] = False
    else:
        payload["phase_evidence"][0][kind] = "NOT_APPLICABLE"
    _rehash_row_and_payload(module, payload, 0)
    report = module.build_report(payload)
    assert report["certified"] is False
    assert report["phase_decisions"][0]["status"] == "FAIL"


def test_any_latency_regression_refuses_even_with_aggregate_reduction():
    module = _module()
    payload = _payload(module)
    payload["phase_evidence"][1]["latency_after_us"] = 101
    payload["phase_evidence"][0]["latency_after_us"] = 80
    _rehash_row_and_payload(module, payload, 0)
    _rehash_row_and_payload(module, payload, 1)
    report = module.build_report(payload)
    assert report["total_latency_after_us"] < report["total_latency_before_us"]
    assert report["certified"] is False
    assert "LATENCY_REGRESSION" in report["phase_decisions"][1]["reasons"]


def test_no_strict_improvement_refuses():
    module = _module()
    payload = _payload(module)
    payload["phase_evidence"][0]["latency_after_us"] = 100
    _rehash_row_and_payload(module, payload, 0)
    report = module.build_report(payload)
    assert report["strict_improvement_count"] == 0
    assert report["certified"] is False


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "fields", "row_hash"])
def test_incomplete_or_malformed_phase_evidence_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["phase_evidence"].pop()
    elif kind == "duplicate":
        payload["phase_evidence"][-1]["phase"] = "4CA"
        _rehash_row_and_payload(module, payload, -1)
    elif kind == "unknown":
        payload["phase_evidence"][-1]["phase"] = "4CZ"
        _rehash_row_and_payload(module, payload, -1)
    elif kind == "fields":
        payload["phase_evidence"][0]["extra"] = True
    else:
        payload["phase_evidence"][0]["evidence_hash"] = "0" * 64
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_outer_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["phase_evidence"][0]["latency_after_us"] = 1
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "certification.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ct_market_data_certification.py"
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
