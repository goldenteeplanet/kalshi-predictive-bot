from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4eg_risk_decision_batch_evaluation.py"
    spec = importlib.util.spec_from_file_location("phase4eg_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    snapshot_hash = "a" * 64
    payload = {
        "schema": module.INPUT_SCHEMA,
        "snapshot": {
            "snapshot_id": "portfolio-v7",
            "artifact_hash": snapshot_hash,
            "freshness_gate_hash": "b" * 64,
            "immutable": True,
            "available_capital": "100.00",
            "current_exposure": "40.00",
            "max_exposure": "100.00",
        },
        "candidates": [
            {
                "candidate_id": "small",
                "snapshot_artifact_hash": snapshot_hash,
                "requested_notional": "50.00",
                "candidate_cap": "50.00",
                "hard_blocked": False,
            },
            {
                "candidate_id": "large",
                "snapshot_artifact_hash": snapshot_hash,
                "requested_notional": "70.00",
                "candidate_cap": "60.00",
                "hard_blocked": False,
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_candidates_are_independent_against_one_immutable_snapshot():
    module = _module()
    report = module.build_report(_payload(module))
    by_id = {row["candidate_id"]: row for row in report["results"]}
    assert by_id["small"]["status"] == "ELIGIBLE"
    assert by_id["large"]["reasons"] == ["CANDIDATE_CAP_EXCEEDED", "PORTFOLIO_EXPOSURE_EXCEEDED"]
    assert all(
        row["available_capital_before"] == row["available_capital_after"]
        for row in report["results"]
    )
    assert all(
        row["current_exposure_before"] == row["current_exposure_after"] for row in report["results"]
    )
    assert report["capital_reserved"] is False
    assert report["snapshot_mutations"] == 0


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("requested_notional", "100.01", "AVAILABLE_CAPITAL_EXCEEDED"),
        ("candidate_cap", "49.99", "CANDIDATE_CAP_EXCEEDED"),
    ],
)
def test_one_cent_beyond_limit_refuses(field, value, reason):
    module = _module()
    payload = _payload(module)
    payload["candidates"] = [payload["candidates"][0]]
    payload["candidates"][0][field] = value
    _rehash(module, payload)
    assert reason in module.build_report(payload)["results"][0]["reasons"]


def test_exact_exposure_boundary_passes():
    module = _module()
    payload = _payload(module)
    payload["candidates"] = [payload["candidates"][0]]
    payload["candidates"][0]["requested_notional"] = "60.00"
    payload["candidates"][0]["candidate_cap"] = "60.00"
    _rehash(module, payload)
    assert module.build_report(payload)["results"][0]["status"] == "ELIGIBLE"


def test_hard_block_is_additive_and_deterministic():
    module = _module()
    payload = _payload(module)
    payload["candidates"][1]["hard_blocked"] = True
    _rehash(module, payload)
    reasons = module.build_report(payload)["results"][0]["reasons"]
    assert reasons == ["HARD_BLOCK", "CANDIDATE_CAP_EXCEEDED", "PORTFOLIO_EXPOSURE_EXCEEDED"]


@pytest.mark.parametrize(
    "kind",
    [
        "mutable",
        "snapshot_hash",
        "snapshot_exposure",
        "empty",
        "duplicate",
        "candidate_snapshot",
        "decimal",
        "hard_block",
        "fields",
    ],
)
def test_malformed_or_mixed_snapshot_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "mutable":
        payload["snapshot"]["immutable"] = False
    elif kind == "snapshot_hash":
        payload["snapshot"]["freshness_gate_hash"] = "bad"
    elif kind == "snapshot_exposure":
        payload["snapshot"]["current_exposure"] = "101"
    elif kind == "empty":
        payload["candidates"] = []
    elif kind == "duplicate":
        payload["candidates"].append(copy.deepcopy(payload["candidates"][0]))
    elif kind == "candidate_snapshot":
        payload["candidates"][0]["snapshot_artifact_hash"] = "c" * 64
    elif kind == "decimal":
        payload["candidates"][0]["requested_notional"] = "NaN"
    elif kind == "hard_block":
        payload["candidates"][0]["hard_blocked"] = 1
    else:
        payload["snapshot"]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_candidate_order_is_canonicalized_and_input_not_mutated():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["candidates"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    second = module.build_report(payload)
    assert first["results"] == second["results"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["snapshot"]["available_capital"] = "99"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "batch.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4eg_risk_decision_batch_evaluation.py"
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
