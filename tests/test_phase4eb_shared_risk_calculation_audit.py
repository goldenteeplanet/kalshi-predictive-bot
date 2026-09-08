from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4eb_shared_risk_calculation_audit.py"
    spec = importlib.util.spec_from_file_location("phase4eb_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(identifier="exposure"):
    return {
        "calculation_id": identifier,
        "consumers": ["POSITION_SIZING", "ADVANCED_RISK"],
        "input_lineage_hash": "a" * 64,
        "semantics_hash": "b" * 64,
        "output_schema_hash": "c" * 64,
        "exact_decimal": True,
        "decision_independent": True,
        "mutable_state": False,
        "consumer_output_coupling": False,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "calculations": [_row(), {**_row("sizing_only"), "consumers": ["POSITION_SIZING"]}],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_safe_shared_calculation_and_independent_decision_are_classified():
    module = _module()
    payload = _payload(module)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert report["safe_reuse_ids"] == ["exposure"]
    assert report["independent_ids"] == ["sizing_only"]
    assert report["decision_outputs_shared"] is False
    assert report["risk_decisions_created"] == 0
    assert payload == original


@pytest.mark.parametrize(
    "field,reason",
    [
        ("exact_decimal", "NON_EXACT_ARITHMETIC"),
        ("decision_independent", "DECISION_SPECIFIC_SEMANTICS"),
        ("mutable_state", "MUTABLE_STATE_DEPENDENCY"),
        ("consumer_output_coupling", "CONSUMER_OUTPUT_COUPLING"),
    ],
)
def test_unsafe_property_keeps_calculation_independent(field, reason):
    module = _module()
    payload = _payload(module)
    payload["calculations"][0][field] = field in {"mutable_state", "consumer_output_coupling"}
    _rehash(module, payload)
    decision = module.build_report(payload)["decisions"][0]
    assert decision["status"] == "KEEP_INDEPENDENT"
    assert reason in decision["reasons"]


@pytest.mark.parametrize(
    "kind", ["empty", "duplicate", "consumer", "duplicate_consumer", "hash", "boolean", "fields"]
)
def test_malformed_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["calculations"] = []
    elif kind == "duplicate":
        payload["calculations"].append(dict(payload["calculations"][0]))
    elif kind == "consumer":
        payload["calculations"][0]["consumers"] = ["UNKNOWN"]
    elif kind == "duplicate_consumer":
        payload["calculations"][0]["consumers"].append("POSITION_SIZING")
    elif kind == "hash":
        payload["calculations"][0]["semantics_hash"] = "bad"
    elif kind == "boolean":
        payload["calculations"][0]["exact_decimal"] = 1
    else:
        payload["calculations"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_canonicalized():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["calculations"].reverse()
    _rehash(module, payload)
    assert first["decisions"] == module.build_report(payload)["decisions"]


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["calculations"][0]["semantics_hash"] = "d" * 64
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4eb_shared_risk_calculation_audit.py"
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
