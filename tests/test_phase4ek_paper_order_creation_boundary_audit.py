from __future__ import annotations

import copy
import importlib.util
import itertools
import json
from pathlib import Path

import pytest


def _module():
    path = (
        Path(__file__).parents[1] / "scripts/local/phase4ek_paper_order_creation_boundary_audit.py"
    )
    spec = importlib.util.spec_from_file_location("phase4ek_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    scenarios = []
    for index, vector in enumerate(itertools.product((False, True), repeat=len(module.GATES))):
        scenario = {
            "scenario_id": f"scenario-{index:02d}",
            "optimization_path_requested": index % 2 == 0,
        }
        scenario.update(dict(zip(module.GATES, vector, strict=True)))
        scenarios.append(scenario)
    payload = {"schema": module.INPUT_SCHEMA, "scenarios": scenarios}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_complete_truth_table_proves_identical_boundary():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "NO_BOUNDARY_BYPASS_PROVEN"
    assert report["truth_table_rows"] == 32
    assert report["boundary_reachable_rows"] == 1
    assert report["optimized_bypasses"] == 0
    assert all(
        row["baseline_boundary_reached"] == row["optimized_boundary_reached"]
        for row in report["truth_table"]
    )
    assert report["paper_order_creation_attempted"] is False
    assert report["paper_orders_created"] == 0


@pytest.mark.parametrize(
    "gate",
    [
        "paper_order_creation_enabled",
        "global_kill_switch_clear",
        "strategy_kill_switch_clear",
        "operator_authorized",
        "routing_eligible",
    ],
)
def test_each_single_failed_gate_blocks_boundary(gate):
    module = _module()
    report = module.build_report(_payload(module))
    row = next(
        row
        for row in report["truth_table"]
        if not row["gate_vector"][gate] and sum(row["gate_vector"].values()) == 4
    )
    assert row["baseline_boundary_reached"] is False
    assert row["optimized_boundary_reached"] is False
    assert row["failed_gates"] == [gate]


def test_optimization_request_does_not_change_any_result():
    module = _module()
    report = module.build_report(_payload(module))
    by_vector = {
        tuple(row["gate_vector"][gate] for gate in module.GATES): row
        for row in report["truth_table"]
    }
    assert all(row["equivalent"] is True for row in by_vector.values())


@pytest.mark.parametrize(
    "kind", ["empty", "missing_row", "duplicate_id", "duplicate_vector", "boolean", "fields"]
)
def test_incomplete_or_malformed_truth_table_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["scenarios"] = []
    elif kind == "missing_row":
        payload["scenarios"].pop()
    elif kind == "duplicate_id":
        payload["scenarios"][1]["scenario_id"] = payload["scenarios"][0]["scenario_id"]
    elif kind == "duplicate_vector":
        for gate in module.GATES:
            payload["scenarios"][1][gate] = payload["scenarios"][0][gate]
    elif kind == "boolean":
        payload["scenarios"][0]["operator_authorized"] = 1
    else:
        payload["scenarios"][0]["extra"] = True
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
    assert first["truth_table"] == second["truth_table"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["scenarios"][0]["routing_eligible"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_order_creation_or_connected_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ek_paper_order_creation_boundary_audit.py"
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
