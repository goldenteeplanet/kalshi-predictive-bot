from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = (
        Path(__file__).parents[1] / "scripts/local/phase4ej_paper_eligibility_handoff_contract.py"
    )
    spec = importlib.util.spec_from_file_location("phase4ej_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "candidate_id": "candidate-1",
        "forecast": {"quality_passed": True, "reason_codes": [], "artifact_hash": "a" * 64},
        "ranking": {"eligible": True, "reason_codes": [], "artifact_hash": "b" * 64},
        "risk": {
            "eligible": True,
            "allowed_quantity": 1,
            "reason_codes": [],
            "artifact_hash": "c" * 64,
        },
        "operator": {"approved": True, "approval_id": "approval-1", "artifact_hash": "d" * 64},
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_all_gates_pass_but_order_creation_remains_unauthorized():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["routing_eligibility"] == {"eligible": True, "reason_codes": []}
    assert report["risk_eligibility"]["allowed_quantity"] == 1
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
    assert report["execution_authorized"] is False


@pytest.mark.parametrize(
    "gate,path",
    [
        ("forecast_quality", ("forecast", "quality_passed")),
        ("ranking_eligibility", ("ranking", "eligible")),
        ("risk_eligibility", ("risk", "eligible")),
        ("operator_approval", ("operator", "approved")),
    ],
)
def test_each_gate_is_independent_and_blocks_routing(gate, path):
    module = _module()
    payload = _payload(module)
    payload[path[0]][path[1]] = False
    if path[0] == "risk":
        payload["risk"]["allowed_quantity"] = 0
    if path[0] == "operator":
        payload["operator"]["approval_id"] = None
    _rehash(module, payload)
    report = module.build_report(payload)
    assert (
        report[gate].get("passed", report[gate].get("eligible", report[gate].get("approved")))
        is False
    )
    assert report["routing_eligibility"]["eligible"] is False
    assert f"{gate.upper()}_FAILED" in report["routing_eligibility"]["reason_codes"]


def test_multiple_failed_gates_have_stable_stage_order():
    module = _module()
    payload = _payload(module)
    payload["forecast"]["quality_passed"] = False
    payload["risk"]["eligible"] = False
    payload["risk"]["allowed_quantity"] = 0
    _rehash(module, payload)
    assert module.build_report(payload)["routing_eligibility"]["reason_codes"] == [
        "FORECAST_QUALITY_FAILED",
        "RISK_ELIGIBILITY_FAILED",
    ]


@pytest.mark.parametrize(
    "kind",
    [
        "candidate",
        "forecast_bool",
        "ranking_hash",
        "risk_quantity_type",
        "eligible_zero",
        "ineligible_nonzero",
        "approved_without_id",
        "unapproved_with_id",
        "duplicate_reason",
        "reason_type",
        "fields",
    ],
)
def test_malformed_or_inconsistent_handoff_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "candidate":
        payload["candidate_id"] = ""
    elif kind == "forecast_bool":
        payload["forecast"]["quality_passed"] = 1
    elif kind == "ranking_hash":
        payload["ranking"]["artifact_hash"] = "bad"
    elif kind == "risk_quantity_type":
        payload["risk"]["allowed_quantity"] = True
    elif kind == "eligible_zero":
        payload["risk"]["allowed_quantity"] = 0
    elif kind == "ineligible_nonzero":
        payload["risk"]["eligible"] = False
    elif kind == "approved_without_id":
        payload["operator"]["approval_id"] = None
    elif kind == "unapproved_with_id":
        payload["operator"]["approved"] = False
    elif kind == "duplicate_reason":
        payload["forecast"]["reason_codes"] = ["A", "A"]
    elif kind == "reason_type":
        payload["ranking"]["reason_codes"] = [1]
    else:
        payload["operator"]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_reason_codes_are_canonicalized_and_input_not_mutated():
    module = _module()
    payload = _payload(module)
    payload["forecast"]["reason_codes"] = ["Z", "A"]
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert report["forecast_quality"]["reason_codes"] == ["A", "Z"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["candidate_id"] = "changed"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "handoff.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ej_paper_eligibility_handoff_contract.py"
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
