from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bq_critical_path_dag.py"
    spec = importlib.util.spec_from_file_location("phase4bq_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module, *, redundant=False):
    nodes = [
        {
            "node": node,
            "expected_duration_ms": index * 100,
            "evidence_hash": module.canonical_hash(node),
        }
        for index, node in enumerate(module.NODES, start=1)
    ]
    edges = [
        {"source": source, "target": target, "reason_hash": module.canonical_hash([source, target])}
        for source, target in module.REQUIRED_EDGES
    ]
    if redundant:
        edges.append(
            {
                "source": module.NODES[0],
                "target": module.NODES[2],
                "reason_hash": module.canonical_hash("extra"),
            }
        )
    payload = {"schema": module.INPUT_SCHEMA, "nodes": nodes, "edges": edges}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_dag_is_complete_deterministic_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.build(payload) == module.build(payload)
    dag, analysis = module.build(payload)
    assert dag["topological_order"] == list(module.NODES)
    assert dag["critical_path"] == list(module.NODES)
    assert analysis["redundant_serial_edge_count"] == 0
    assert dag["execution_authorized"] is False


def test_redundant_hidden_serial_dependency_is_reported():
    module = _module()
    _, analysis = module.build(_payload(module, redundant=True))
    assert analysis["redundant_serial_edges"] == [
        {"source": module.NODES[0], "target": module.NODES[2]}
    ]


@pytest.mark.parametrize(
    "kind", ("missing_node", "reordered_node", "missing_edge", "duplicate_edge")
)
def test_coverage_order_and_duplicates_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing_node":
        payload["nodes"].pop()
    elif kind == "reordered_node":
        payload["nodes"].reverse()
    elif kind == "missing_edge":
        payload["edges"].pop()
    else:
        payload["edges"].append(dict(payload["edges"][0]))
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build(payload)


def test_cycle_unknown_endpoint_self_edge_and_bad_evidence_fail_closed():
    module = _module()
    cases = (
        lambda p: p["edges"].append(
            {"source": module.NODES[-1], "target": module.NODES[0], "reason_hash": "a" * 64}
        ),
        lambda p: p["edges"][0].update(source="UNKNOWN"),
        lambda p: p["edges"][0].update(target=module.NODES[0]),
        lambda p: p["nodes"][0].update(evidence_hash="bad"),
    )
    for mutate in cases:
        payload = _payload(module)
        mutate(payload)
        _rehash(module, payload)
        with pytest.raises(ValueError):
            module.build(payload)


@pytest.mark.parametrize("duration", (-1, 3_600_001, 1.5, True))
def test_duration_bound_and_type_fail_closed(duration):
    module = _module()
    payload = _payload(module)
    payload["nodes"][0]["expected_duration_ms"] = duration
    _rehash(module, payload)
    with pytest.raises(ValueError, match="DURATION"):
        module.build(payload)


def test_tampering_extra_fields_and_edge_evidence_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(payload)
    payload = _payload(module)
    payload["nodes"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build(payload)
    payload = _payload(module)
    payload["edges"][0]["reason_hash"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="EVIDENCE"):
        module.build(payload)


def test_source_is_artifact_only():
    source = (Path(__file__).parents[1] / "scripts/local/phase4bq_critical_path_dag.py").read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
