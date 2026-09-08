from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4el_paper_routing_simulator.py"
    spec = importlib.util.spec_from_file_location("phase4el_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _intent(identifier="intent-a", key="key-a", quantity=3, price=50, available=3):
    return {
        "intent_id": identifier,
        "idempotency_key": key,
        "handoff_artifact_hash": "a" * 64,
        "quantity": quantity,
        "price_cents": price,
        "available_fill_quantity": available,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "policy": {"max_quantity": 5, "min_price_cents": 10, "max_price_cents": 90},
        "stage_latency_ms": {
            "duplicate_check": 1,
            "quantity_check": 2,
            "price_check": 3,
            "fill_model": 4,
        },
        "intents": [_intent()],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


@pytest.mark.parametrize(
    "available,status,filled,remaining",
    [(3, "FULL_FILL", 3, 0), (2, "PARTIAL_FILL", 2, 1), (0, "UNFILLED", 0, 3)],
)
def test_deterministic_synthetic_fill_outcomes(available, status, filled, remaining):
    module = _module()
    payload = _payload(module)
    payload["intents"][0]["available_fill_quantity"] = available
    _rehash(module, payload)
    row = module.build_report(payload)["results"][0]
    assert row["fill_status"] == status
    assert row["simulated_filled_quantity"] == filled
    assert row["simulated_remaining_quantity"] == remaining
    assert row["simulated_latency_ms"] == 10


def test_duplicate_primary_is_canonical_and_input_order_independent():
    module = _module()
    payload = _payload(module)
    payload["intents"] = [_intent("intent-z", "same"), _intent("intent-a", "same")]
    _rehash(module, payload)
    report = module.build_report(payload)
    by_id = {row["intent_id"]: row for row in report["results"]}
    assert by_id["intent-a"]["status"] == "SIMULATED"
    assert by_id["intent-z"]["reasons"] == ["DUPLICATE_INTENT"]
    assert by_id["intent-z"]["stages_run"] == ["duplicate_check"]


@pytest.mark.parametrize(
    "kind,reason,stages,latency",
    [
        ("quantity", "QUANTITY_LIMIT_EXCEEDED", ["duplicate_check", "quantity_check"], 3),
        (
            "low_price",
            "PRICE_OUT_OF_BOUNDS",
            ["duplicate_check", "quantity_check", "price_check"],
            6,
        ),
        (
            "high_price",
            "PRICE_OUT_OF_BOUNDS",
            ["duplicate_check", "quantity_check", "price_check"],
            6,
        ),
    ],
)
def test_quantity_and_price_refusals_stop_at_correct_stage(kind, reason, stages, latency):
    module = _module()
    payload = _payload(module)
    if kind == "quantity":
        payload["intents"][0]["quantity"] = 6
    elif kind == "low_price":
        payload["intents"][0]["price_cents"] = 9
    else:
        payload["intents"][0]["price_cents"] = 91
    _rehash(module, payload)
    row = module.build_report(payload)["results"][0]
    assert row["reasons"] == [reason]
    assert row["stages_run"] == stages
    assert row["simulated_latency_ms"] == latency
    assert row["fill_status"] == "NOT_MODELED"


@pytest.mark.parametrize("price", [10, 90])
def test_exact_price_boundaries_pass(price):
    module = _module()
    payload = _payload(module)
    payload["intents"][0]["price_cents"] = price
    _rehash(module, payload)
    assert module.build_report(payload)["results"][0]["status"] == "SIMULATED"


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "duplicate_id",
        "key",
        "hash",
        "quantity",
        "price",
        "fill",
        "policy",
        "latency",
        "fields",
    ],
)
def test_malformed_simulation_input_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["intents"] = []
    elif kind == "duplicate_id":
        payload["intents"].append(copy.deepcopy(payload["intents"][0]))
        payload["intents"][1]["idempotency_key"] = "different"
    elif kind == "key":
        payload["intents"][0]["idempotency_key"] = ""
    elif kind == "hash":
        payload["intents"][0]["handoff_artifact_hash"] = "bad"
    elif kind == "quantity":
        payload["intents"][0]["quantity"] = True
    elif kind == "price":
        payload["intents"][0]["price_cents"] = 0
    elif kind == "fill":
        payload["intents"][0]["available_fill_quantity"] = -1
    elif kind == "policy":
        payload["policy"]["max_price_cents"] = 100
    elif kind == "latency":
        payload["stage_latency_ms"]["fill_model"] = -1
    else:
        payload["intents"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_canonicalized_and_not_mutated():
    module = _module()
    payload = _payload(module)
    payload["intents"] = [_intent("b", "b"), _intent("a", "a")]
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert [row["intent_id"] for row in report["results"]] == ["a", "b"]
    assert payload == original


def test_tampering_determinism_atomic_publication_and_no_real_writes(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["intents"][0]["quantity"] = 4
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    assert report["real_paper_orders_created"] == 0
    assert report["real_paper_fills_created"] == 0
    assert report["database_writes"] == 0
    output = tmp_path / "simulation.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_order_creation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4el_paper_routing_simulator.py"
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
