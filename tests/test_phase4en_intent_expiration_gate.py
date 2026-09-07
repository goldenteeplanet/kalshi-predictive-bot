from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4en_intent_expiration_gate.py"
    spec = importlib.util.spec_from_file_location("phase4en_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _intent(identifier="intent-a", expiration="2026-08-26T12:00:01.500Z"):
    return {
        "intent_id": identifier,
        "handoff_artifact_hash": "a" * 64,
        "intent_expires_at": expiration,
        "evidence_expires_at": expiration,
        "approval_expires_at": expiration,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "as_of": "2026-08-26T12:00:00.000Z",
        "routing_duration_ms": 1000,
        "safety_margin_ms": 500,
        "intents": [_intent()],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_completion_boundary_passes_with_zero_headroom():
    module = _module()
    report = module.build_report(_payload(module))
    row = report["results"][0]
    assert report["required_completion_at"] == "2026-08-26T12:00:01.500Z"
    assert row["status"] == "ELIGIBLE_FOR_SIMULATED_ROUTING"
    assert row["intent_expires_headroom_ms"] == 0
    assert row["evidence_expires_headroom_ms"] == 0
    assert row["approval_expires_headroom_ms"] == 0
    assert report["routing_attempted"] is False


@pytest.mark.parametrize(
    "field,reason",
    [
        ("intent_expires_at", "INTENT_EXPIRES_BEFORE_ROUTING"),
        ("evidence_expires_at", "EVIDENCE_EXPIRES_BEFORE_ROUTING"),
        ("approval_expires_at", "APPROVAL_EXPIRES_BEFORE_ROUTING"),
    ],
)
def test_one_millisecond_shortfall_refuses(field, reason):
    module = _module()
    payload = _payload(module)
    payload["intents"][0][field] = "2026-08-26T12:00:01.499Z"
    _rehash(module, payload)
    row = module.build_report(payload)["results"][0]
    assert row["status"] == "REFUSE"
    assert row["reasons"] == [reason]
    assert row[field.removesuffix("_at") + "_headroom_ms"] == -1


def test_multiple_expirations_have_fixed_reason_order():
    module = _module()
    payload = _payload(module)
    for field, _ in module.EXPIRATIONS:
        payload["intents"][0][field] = "2026-08-26T12:00:00.000Z"
    _rehash(module, payload)
    assert module.build_report(payload)["results"][0]["reasons"] == [
        reason for _, reason in module.EXPIRATIONS
    ]


def test_zero_duration_and_margin_require_validity_at_as_of():
    module = _module()
    payload = _payload(module)
    payload["routing_duration_ms"] = 0
    payload["safety_margin_ms"] = 0
    payload["intents"] = [_intent(expiration=payload["as_of"])]
    _rehash(module, payload)
    assert module.build_report(payload)["results"][0]["status"] == "ELIGIBLE_FOR_SIMULATED_ROUTING"


@pytest.mark.parametrize(
    "kind",
    ["empty", "duplicate", "as_of", "offset", "expiration", "hash", "duration", "margin", "fields"],
)
def test_malformed_expiration_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["intents"] = []
    elif kind == "duplicate":
        payload["intents"].append(copy.deepcopy(payload["intents"][0]))
    elif kind == "as_of":
        payload["as_of"] = "bad"
    elif kind == "offset":
        payload["intents"][0]["intent_expires_at"] = "2026-08-26T06:00:01.500-06:00"
    elif kind == "expiration":
        payload["intents"][0]["approval_expires_at"] = "bad"
    elif kind == "hash":
        payload["intents"][0]["handoff_artifact_hash"] = "bad"
    elif kind == "duration":
        payload["routing_duration_ms"] = True
    elif kind == "margin":
        payload["safety_margin_ms"] = -1
    else:
        payload["intents"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_canonicalized_and_not_mutated():
    module = _module()
    payload = _payload(module)
    payload["intents"] = [_intent("z"), _intent("a")]
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert [row["intent_id"] for row in report["results"]] == ["a", "z"]
    assert payload == original


def test_tampering_determinism_atomic_publication_and_no_orders(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["routing_duration_ms"] = 999
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    assert report["paper_orders_created"] == 0
    assert report["production_records_created"] == 0
    output = tmp_path / "expiration.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_order_creation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4en_intent_expiration_gate.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "insert_order",
        "/home/james",
    ):
        assert token not in source
