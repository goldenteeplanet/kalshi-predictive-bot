from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cz_workstream_ii_gate.py"
    spec = importlib.util.spec_from_file_location("phase4cz_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    rows = []
    for phase in module.REQUIRED_PHASES:
        row = {
            "phase": phase,
            "status": "COMPLETE",
            "lineage": "PASS",
            "deterministic_replay": "PASS",
            "resource_bounds": "PASS",
            "safety": "PASS",
        }
        row["evidence_hash"] = module._hash(row)
        rows.append(row)
    payload = {
        "schema": module.INPUT_SCHEMA,
        "phase_evidence": rows,
        "changed_paths": [
            "docs/phase4ca-catalog-delta-planner.md",
            "scripts/local/phase4cy_partial_failure_isolation.py",
            "tests/test_phase4cx_bounded_batch_planner.py",
            "docs/phase4-roadmap-progress.md",
        ],
        "cumulative_tests_passed": 1173,
        "expected_platform_skips": 2,
        "ruff_passed": True,
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def _rehash_row(module, payload, index):
    payload["phase_evidence"][index]["evidence_hash"] = module._hash(
        payload["phase_evidence"][index]
    )
    _rehash(module, payload)


def test_complete_safe_workstream_passes_final_gate():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "WORKSTREAM_II_FINAL_GATE_PASS"
    assert report["passed"] is True
    assert len(report["verified_phases"]) == 25
    assert report["prohibited_paths"] == []
    assert report["production_collector_changes"] == 0
    assert report["advancement_authorized"] is True
    assert report["trading_execution_authorized"] is False


@pytest.mark.parametrize(
    "field", ["status", "lineage", "deterministic_replay", "resource_bounds", "safety"]
)
def test_any_incomplete_phase_proof_fails_closed(field: str):
    module = _module()
    payload = _payload(module)
    payload["phase_evidence"][0][field] = "FAIL"
    _rehash_row(module, payload, 0)
    with pytest.raises(ValueError, match="PROOF"):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "fields", "row_hash"])
def test_incomplete_or_malformed_phase_set_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["phase_evidence"].pop()
    elif kind == "duplicate":
        payload["phase_evidence"][-1]["phase"] = "4CA"
        _rehash_row(module, payload, -1)
    elif kind == "unknown":
        payload["phase_evidence"][-1]["phase"] = "4CZ"
        _rehash_row(module, payload, -1)
    elif kind == "fields":
        payload["phase_evidence"][0]["extra"] = True
    else:
        payload["phase_evidence"][0]["evidence_hash"] = "0" * 64
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize(
    "path",
    [
        "src/kalshi_predictor/ingest/markets.py",
        "scripts/local/kalshi-fixed-rate-refresh.sh",
        "deploy/systemd/collector.service",
        "../outside.py",
        "C:\\outside.py",
    ],
)
def test_production_or_unsafe_path_refuses_gate(path: str):
    module = _module()
    payload = _payload(module)
    payload["changed_paths"].append(path)
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    assert path in report["prohibited_paths"]
    assert report["advancement_authorized"] is False


@pytest.mark.parametrize("kind", ["paths", "duplicate_path", "tests", "skips", "ruff"])
def test_malformed_or_failed_top_level_gate_evidence(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "paths":
        payload["changed_paths"] = []
    elif kind == "duplicate_path":
        payload["changed_paths"].append(payload["changed_paths"][0])
    elif kind == "tests":
        payload["cumulative_tests_passed"] = 0
    elif kind == "skips":
        payload["expected_platform_skips"] = -1
    else:
        payload["ruff_passed"] = False
    _rehash(module, payload)
    if kind == "ruff":
        assert module.build_report(payload)["status"] == "REFUSE"
    else:
        with pytest.raises(ValueError):
            module.build_report(payload)


def test_outer_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["cumulative_tests_passed"] = 9999
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "gate.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cz_workstream_ii_gate.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
