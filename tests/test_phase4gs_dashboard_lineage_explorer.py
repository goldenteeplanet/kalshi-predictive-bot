from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_lineage_explorer import (
    DashboardLineageExplorerError,
    build_dashboard_lineage_explorer,
    make_dashboard_lineage_edge,
    make_dashboard_lineage_node,
    validate_dashboard_lineage_explorer,
)


def test_valid_lineage_is_deterministic_bounded_and_read_only() -> None:
    first = _explorer(nodes=list(reversed(_nodes())), edges=list(reversed(_edges())))
    second = _explorer(nodes=_nodes(), edges=_edges())
    validate_dashboard_lineage_explorer(first)
    assert first.status == "READY"
    assert first.traversal_order == ("source", "gate", "panel")
    assert first.explorer_hash == second.explorer_hash
    assert first.execution_authorized is False


def test_empty_missing_endpoint_and_bounds_fail_closed() -> None:
    with pytest.raises(DashboardLineageExplorerError, match="NODES_EMPTY"):
        _explorer(nodes=[], edges=[])
    with pytest.raises(DashboardLineageExplorerError, match="EDGE_ENDPOINT_MISSING"):
        _explorer(nodes=_nodes()[:-1], edges=_edges())
    with pytest.raises(DashboardLineageExplorerError, match="NODE_BOUND_EXCEEDED"):
        _explorer(nodes=_nodes(), edges=_edges(), max_nodes=2)


def test_exact_freshness_boundary_is_ready_and_one_second_over_is_stale() -> None:
    assert _explorer(nodes=_nodes(age=300), edges=_edges()).status == "READY"
    stale = _explorer(nodes=_nodes(age=301), edges=_edges())
    assert stale.status == "STALE"
    assert stale.reasons == ("LINEAGE_EVIDENCE_STALE",)


def test_unavailable_node_is_incomplete_and_disconnected_graph_fails() -> None:
    nodes = _nodes()
    nodes[-1] = _node("panel", 3, available=False)
    incomplete = _explorer(nodes=nodes, edges=_edges())
    assert incomplete.status == "INCOMPLETE"
    assert incomplete.reasons == ("NODE_UNAVAILABLE:panel",)
    with pytest.raises(DashboardLineageExplorerError, match="LINEAGE_DISCONNECTED"):
        _explorer(nodes=_nodes(), edges=_edges()[:-1])


def test_malformed_sequence_cycle_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardLineageExplorerError, match="NODE_FIELD_INVALID"):
        _node("source", -1)
    bad_edge = make_dashboard_lineage_edge(
        parent_id="panel", child_id="source", relationship="cycle"
    )
    with pytest.raises(DashboardLineageExplorerError, match="LINEAGE_SEQUENCE_INVALID"):
        _explorer(nodes=_nodes(), edges=_edges() + [bad_edge])
    mixed = _nodes()
    mixed[-1] = _node("panel", 3, identity="b" * 64)
    with pytest.raises(DashboardLineageExplorerError, match="NODE_LINEAGE_MIXED"):
        _explorer(nodes=mixed, edges=_edges())
    item = _node("source", 1)
    with pytest.raises(DashboardLineageExplorerError, match="NODE_HASH_MISMATCH"):
        _explorer(nodes=[replace(item, available=False), *_nodes()[1:]], edges=_edges())


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = _explorer(nodes=_nodes(), edges=_edges())
    with pytest.raises(DashboardLineageExplorerError, match="EXPLORER_HASH_MISMATCH"):
        validate_dashboard_lineage_explorer(replace(result, explorer_hash="0" * 64))
    with pytest.raises(DashboardLineageExplorerError, match="EXPLORER_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_lineage_explorer(replace(result, execution_authorized=True))


def test_explorer_has_no_query_publication_or_mutation_surface() -> None:
    names = set(build_dashboard_lineage_explorer.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "publish", "replace", "unlink", "write"}
    )


def _node(node_id, sequence, *, age=1, available=True, identity="a" * 64):
    return make_dashboard_lineage_node(
        node_id=node_id,
        node_kind="artifact",
        artifact_schema_version="v1",
        artifact_hash="hash:" + node_id,
        source_identity_hash=identity,
        source_watermark="w",
        sequence=sequence,
        evidence_age_seconds=age,
        available=available,
    )


def _nodes(*, age=1):
    return [_node("source", 1, age=age), _node("gate", 2, age=age), _node("panel", 3, age=age)]


def _edges():
    return [
        make_dashboard_lineage_edge(parent_id="source", child_id="gate", relationship="feeds"),
        make_dashboard_lineage_edge(parent_id="gate", child_id="panel", relationship="renders"),
    ]


def _explorer(*, nodes, edges, **kwargs):
    return build_dashboard_lineage_explorer(
        nodes=nodes, edges=edges, root_node_id="source", **kwargs
    )
