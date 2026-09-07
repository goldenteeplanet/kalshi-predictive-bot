from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4em_duplicate_intent_suppression.py"
    spec = importlib.util.spec_from_file_location("phase4em_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _intent(identifier="intent-a"):
    return {
        "intent_id": identifier,
        "database_identity": "prod-db-instance-1",
        "forecast_artifact_hash": "a" * 64,
        "snapshot_artifact_hash": "b" * 64,
        "ranking_artifact_hash": "c" * 64,
        "position_sizing_artifact_hash": "d" * 64,
        "advanced_risk_artifact_hash": "e" * 64,
        "approval_artifact_hash": "f" * 64,
        "market_ticker": "KXTEST",
        "side": "YES",
        "quantity": 1,
        "price_cents": 50,
        "expires_at": "2026-08-26T13:00:00.000Z",
    }


def _payload(module):
    payload = {"schema": module.INPUT_SCHEMA, "intents": [_intent()]}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_identical_lineage_and_terms_suppress_duplicate_canonically():
    module = _module()
    payload = _payload(module)
    payload["intents"] = [_intent("intent-z"), _intent("intent-a")]
    _rehash(module, payload)
    report = module.build_report(payload)
    by_id = {row["intent_id"]: row for row in report["results"]}
    assert by_id["intent-a"]["status"] == "PRIMARY"
    assert by_id["intent-z"]["status"] == "SUPPRESS_DUPLICATE"
    assert by_id["intent-z"]["primary_intent_id"] == "intent-a"
    assert report["suppressed_duplicate_count"] == 1
    assert report["idempotency_records_written"] == 0


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("database_identity", "other-db"),
        ("forecast_artifact_hash", "0" * 64),
        ("snapshot_artifact_hash", "1" * 64),
        ("ranking_artifact_hash", "2" * 64),
        ("position_sizing_artifact_hash", "3" * 64),
        ("advanced_risk_artifact_hash", "4" * 64),
        ("approval_artifact_hash", "5" * 64),
        ("market_ticker", "KXOTHER"),
        ("side", "NO"),
        ("quantity", 2),
        ("price_cents", 51),
        ("expires_at", "2026-08-26T13:00:00.001Z"),
    ],
)
def test_changing_any_identity_component_changes_key(field, replacement):
    module = _module()
    payload = _payload(module)
    second = _intent("intent-b")
    second[field] = replacement
    payload["intents"].append(second)
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["primary_count"] == 2
    assert report["results"][0]["idempotency_key"] != report["results"][1]["idempotency_key"]


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "duplicate_id",
        "database",
        "hash",
        "market",
        "side",
        "quantity",
        "price_low",
        "price_high",
        "expiration",
        "offset",
        "fields",
    ],
)
def test_malformed_identity_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    intent = payload["intents"][0]
    if kind == "empty":
        payload["intents"] = []
    elif kind == "duplicate_id":
        payload["intents"].append(copy.deepcopy(intent))
        payload["intents"][1]["market_ticker"] = "OTHER"
    elif kind == "database":
        intent["database_identity"] = ""
    elif kind == "hash":
        intent["approval_artifact_hash"] = "bad"
    elif kind == "market":
        intent["market_ticker"] = ""
    elif kind == "side":
        intent["side"] = "BUY"
    elif kind == "quantity":
        intent["quantity"] = True
    elif kind == "price_low":
        intent["price_cents"] = 0
    elif kind == "price_high":
        intent["price_cents"] = 100
    elif kind == "expiration":
        intent["expires_at"] = "bad"
    elif kind == "offset":
        intent["expires_at"] = "2026-08-26T07:00:00-06:00"
    else:
        intent["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_irrelevant_and_not_mutated():
    module = _module()
    payload = _payload(module)
    payload["intents"] = [_intent("z"), _intent("a")]
    _rehash(module, payload)
    first = module.build_report(payload)
    payload["intents"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    second = module.build_report(payload)
    assert first["results"] == second["results"]
    assert payload == original


def test_tampering_determinism_atomic_publication_and_no_writes(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["intents"][0]["quantity"] = 2
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    assert report["paper_orders_created"] == 0
    assert report["production_records_created"] == 0
    output = tmp_path / "idempotency.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_order_creation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4em_duplicate_intent_suppression.py"
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
