from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ea_risk_decision_dependency_graph.py"
    spec = importlib.util.spec_from_file_location("phase4ea_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    nodes = [
        {
            "node_id": "exposure",
            "kind": "CALCULATION",
            "decision": None,
            "dependencies": ["portfolio"],
            "work_units": 3,
        },
        {
            "node_id": "size_cap",
            "kind": "CAP",
            "decision": None,
            "dependencies": ["exposure", "policy"],
            "work_units": 2,
        },
        {
            "node_id": "loss_block",
            "kind": "HARD_BLOCK",
            "decision": None,
            "dependencies": ["exposure", "policy"],
            "work_units": 1,
        },
        {
            "node_id": "liquidity_cap",
            "kind": "CAP",
            "decision": None,
            "dependencies": ["market"],
            "work_units": 2,
        },
        {
            "node_id": "liquidity_block",
            "kind": "HARD_BLOCK",
            "decision": None,
            "dependencies": ["market"],
            "work_units": 1,
        },
        {
            "node_id": "position_decision",
            "kind": "DECISION",
            "decision": "POSITION_SIZING",
            "dependencies": ["size_cap", "loss_block"],
            "work_units": 1,
        },
        {
            "node_id": "risk_decision",
            "kind": "DECISION",
            "decision": "ADVANCED_RISK",
            "dependencies": ["size_cap", "loss_block", "liquidity_cap", "liquidity_block"],
            "work_units": 1,
        },
    ]
    payload = {
        "schema": module.INPUT_SCHEMA,
        "inputs": [
            {
                "input_id": "portfolio",
                "source_phase": "snapshot",
                "artifact_hash": "a" * 64,
                "immutable": True,
            },
            {
                "input_id": "policy",
                "source_phase": "configuration",
                "artifact_hash": "b" * 64,
                "immutable": True,
            },
            {
                "input_id": "market",
                "source_phase": "ranking",
                "artifact_hash": "c" * 64,
                "immutable": True,
            },
        ],
        "nodes": nodes,
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_graph_maps_order_controls_shared_calculations_and_lineage():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["topological_order"].index("exposure") < report["topological_order"].index(
        "size_cap"
    )
    assert report["shared_calculations"] == ["exposure"]
    decisions = {row["decision"]: row for row in report["decision_lineage"]}
    assert decisions["POSITION_SIZING"]["upstream_inputs"] == ["policy", "portfolio"]
    assert decisions["ADVANCED_RISK"]["upstream_inputs"] == ["market", "policy", "portfolio"]
    assert report["risk_decisions_created"] == 0


def test_input_order_does_not_change_resolved_graph():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["inputs"].reverse()
    payload["nodes"].reverse()
    _rehash(module, payload)
    second = module.build_report(payload)
    assert first["topological_order"] == second["topological_order"]
    assert first["nodes"] == second["nodes"]


@pytest.mark.parametrize("kind", ["cycle", "missing", "duplicate_dependency", "empty_dependency"])
def test_invalid_dependency_graph_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "cycle":
        payload["nodes"][0]["dependencies"] = ["size_cap"]
    elif kind == "missing":
        payload["nodes"][0]["dependencies"] = ["missing"]
    elif kind == "duplicate_dependency":
        payload["nodes"][0]["dependencies"] = ["portfolio", "portfolio"]
    else:
        payload["nodes"][0]["dependencies"] = []
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize(
    "kind", ["missing_decision", "duplicate_decision", "missing_cap", "missing_block"]
)
def test_decision_or_control_omission_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "missing_decision":
        payload["nodes"].pop()
    elif kind == "duplicate_decision":
        payload["nodes"][-1]["decision"] = "POSITION_SIZING"
    elif kind == "missing_cap":
        payload["nodes"][-2]["dependencies"] = ["loss_block"]
    else:
        payload["nodes"][-2]["dependencies"] = ["size_cap"]
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize(
    "kind", ["hash", "mutable", "node_kind", "nondecision_label", "work", "duplicate_input"]
)
def test_malformed_nodes_fail_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "hash":
        payload["inputs"][0]["artifact_hash"] = "bad"
    elif kind == "mutable":
        payload["inputs"][0]["immutable"] = False
    elif kind == "node_kind":
        payload["nodes"][0]["kind"] = "WRITE"
    elif kind == "nondecision_label":
        payload["nodes"][0]["decision"] = "ADVANCED_RISK"
    elif kind == "work":
        payload["nodes"][0]["work_units"] = True
    else:
        payload["inputs"][1]["input_id"] = "portfolio"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["nodes"][0]["work_units"] = 4
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "graph.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ea_risk_decision_dependency_graph.py"
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
