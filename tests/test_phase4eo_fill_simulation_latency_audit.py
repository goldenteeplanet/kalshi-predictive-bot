from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4eo_fill_simulation_latency_audit.py"
    spec = importlib.util.spec_from_file_location("phase4eo_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scenario(identifier, model, requested=10, available=8, queue=0, bps=0, snapshot="a"):
    return {
        "scenario_id": identifier,
        "model": model,
        "book_snapshot_hash": snapshot * 64,
        "requested_quantity": requested,
        "available_quantity": available,
        "queue_ahead_quantity": queue,
        "participation_bps": bps,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "scenarios": [
            _scenario("top", "TOP_OF_BOOK"),
            _scenario("queue", "QUEUE_AHEAD", queue=3),
            _scenario("pro-rata", "PRO_RATA", bps=2500),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_all_models_have_exact_legacy_optimized_equivalence():
    module = _module()
    report = module.build_report(_payload(module))
    by_id = {row["scenario_id"]: row for row in report["scenarios"]}
    assert by_id["top"]["optimized_filled_quantity"] == 8
    assert by_id["queue"]["optimized_filled_quantity"] == 5
    assert by_id["pro-rata"]["optimized_filled_quantity"] == 2
    assert all(
        row["legacy_filled_quantity"] == row["optimized_filled_quantity"]
        for row in report["scenarios"]
    )
    assert report["real_paper_fills_modified"] == 0
    assert report["database_writes"] == 0


@pytest.mark.parametrize(
    "available,bps,expected", [(1, 9999, 0), (1, 10000, 1), (3, 3333, 0), (3, 3334, 1), (100, 1, 0)]
)
def test_pro_rata_integer_basis_point_boundaries(available, bps, expected):
    module = _module()
    payload = _payload(module)
    payload["scenarios"] = [
        _scenario("boundary", "PRO_RATA", requested=100, available=available, bps=bps)
    ]
    _rehash(module, payload)
    assert module.build_report(payload)["scenarios"][0]["optimized_filled_quantity"] == expected


@pytest.mark.parametrize("queue,expected", [(0, 8), (8, 0), (9, 0)])
def test_queue_ahead_boundaries(queue, expected):
    module = _module()
    payload = _payload(module)
    payload["scenarios"] = [_scenario("queue", "QUEUE_AHEAD", queue=queue)]
    _rehash(module, payload)
    assert module.build_report(payload)["scenarios"][0]["optimized_filled_quantity"] == expected


def test_shared_book_model_setup_is_reused_in_work_units():
    module = _module()
    payload = _payload(module)
    payload["scenarios"] = [_scenario("a", "TOP_OF_BOOK"), _scenario("b", "TOP_OF_BOOK")]
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["legacy_work_units"] == 14
    assert report["optimized_work_units"] == 7
    assert report["work_reduction_units"] == 7
    assert report["wall_clock_used_as_gate"] is False


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "duplicate",
        "model",
        "hash",
        "requested",
        "available",
        "queue",
        "bps",
        "unused_queue",
        "unused_bps",
        "fields",
    ],
)
def test_malformed_scenario_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    scenario = payload["scenarios"][0]
    if kind == "empty":
        payload["scenarios"] = []
    elif kind == "duplicate":
        payload["scenarios"].append(copy.deepcopy(scenario))
    elif kind == "model":
        scenario["model"] = "RANDOM"
    elif kind == "hash":
        scenario["book_snapshot_hash"] = "bad"
    elif kind == "requested":
        scenario["requested_quantity"] = 0
    elif kind == "available":
        scenario["available_quantity"] = True
    elif kind == "queue":
        payload["scenarios"][1]["queue_ahead_quantity"] = -1
    elif kind == "bps":
        payload["scenarios"][2]["participation_bps"] = 10001
    elif kind == "unused_queue":
        scenario["queue_ahead_quantity"] = 1
    elif kind == "unused_bps":
        scenario["participation_bps"] = 1
    else:
        scenario["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_canonicalized_and_not_mutated():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["scenarios"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    second = module.build_report(payload)
    assert first["scenarios"] == second["scenarios"]
    assert payload == original


def test_tampering_determinism_atomic_publication_and_no_real_fills(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["scenarios"][0]["available_quantity"] = 9
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    assert report["real_paper_fills_created"] == 0
    output = tmp_path / "benchmark.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_fill_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4eo_fill_simulation_latency_audit.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_fill",
        "insert_fill",
        "/home/james",
    ):
        assert token not in source
