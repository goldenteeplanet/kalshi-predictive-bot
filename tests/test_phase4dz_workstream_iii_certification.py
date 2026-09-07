from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4dz_workstream_iii_certification.py"
    spec = importlib.util.spec_from_file_location("phase4dz_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    rows = []
    for phase in module.REQUIRED_PHASES:
        row = {"phase": phase, "status": "COMPLETE"}
        row.update({field: "PASS" for field in module.PROOF_FIELDS})
        row["evidence_hash"] = module._hash(row)
        rows.append(row)
    payload = {
        "schema": module.INPUT_SCHEMA,
        "phase_evidence": rows,
        "changed_paths": [
            "docs/phase4da-forecast-dependency-graph.md",
            "scripts/local/phase4dy_forecast_ranking_residual_risk_review.py",
            "tests/test_phase4dx_forecast_pipeline_differential_replay.py",
            "docs/phase4-roadmap-progress.md",
        ],
        "cumulative_tests_passed": 1612,
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


def test_complete_workstream_certifies_offline_and_paper_only():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "WORKSTREAM_III_OFFLINE_PAPER_CERTIFIED"
    assert report["certified"] is True
    assert len(report["verified_phases"]) == 25
    assert report["offline_evaluation_authorized"] is True
    assert report["paper_evaluation_authorized"] is True
    assert report["production_deployment_authorized"] is False
    assert report["trading_execution_authorized"] is False


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "forecast_equivalence",
        "ranking_equivalence",
        "lineage",
        "resource_bounds",
        "offline_paper_safety",
    ],
)
def test_any_incomplete_proof_fails_closed(field):
    module = _module()
    payload = _payload(module)
    payload["phase_evidence"][0][field] = "FAIL"
    _rehash_row(module, payload, 0)
    with pytest.raises(ValueError, match="PROOF"):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "fields", "row_hash"])
def test_malformed_phase_set_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["phase_evidence"].pop()
    elif kind == "duplicate":
        payload["phase_evidence"][-1]["phase"] = "4DA"
        _rehash_row(module, payload, -1)
    elif kind == "unknown":
        payload["phase_evidence"][-1]["phase"] = "4DZ"
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
        "src/kalshi_predictor/forecast.py",
        "deploy/systemd/writer.service",
        "../outside.py",
        "C:\\outside.py",
    ],
)
def test_production_or_unsafe_path_refuses(path):
    module = _module()
    payload = _payload(module)
    payload["changed_paths"].append(path)
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    assert path in report["prohibited_paths"]
    assert report["next_workstream_authorized"] is False


@pytest.mark.parametrize("kind", ["paths", "duplicate_path", "tests", "skips", "ruff"])
def test_invalid_top_level_evidence_fails_or_refuses(kind):
    module = _module()
    payload = _payload(module)
    if kind == "paths":
        payload["changed_paths"] = []
    elif kind == "duplicate_path":
        payload["changed_paths"].append(payload["changed_paths"][0])
    elif kind == "tests":
        payload["cumulative_tests_passed"] = True
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


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["cumulative_tests_passed"] += 1
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "certification.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dz_workstream_iii_certification.py"
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
