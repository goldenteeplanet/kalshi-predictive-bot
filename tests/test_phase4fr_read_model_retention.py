from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from kalshi_predictor.phase4cd.read_model_chain import build_chain_node
from kalshi_predictor.phase4cd.read_model_retention import (
    RetentionPolicyError,
    plan_retention,
    validate_retention_plan,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def test_safe_prefix_retention_preserves_boundary_anchor() -> None:
    nodes = _chain(5)
    plan = _plan(nodes, keep_last=3, max_remove=2)
    assert plan["remove_node_hashes"] == [nodes[0]["node_hash"], nodes[1]["node_hash"]]
    assert plan["retain_node_hashes"] == [node["node_hash"] for node in nodes[2:]]
    assert plan["retention_anchor"]["removed_head_hash"] == nodes[1]["node_hash"]
    assert plan["retention_anchor"]["retained_first_previous_hash"] == nodes[1]["node_hash"]
    _validate_plan(plan, nodes, now=NOW + timedelta(seconds=5))


def test_noop_retention_has_no_anchor() -> None:
    nodes = _chain(2)
    plan = _plan(nodes, keep_last=2, max_remove=0)
    assert plan["remove_node_hashes"] == []
    assert plan["retention_anchor"] is None
    assert plan["operation"] == "PLAN_ONLY_NO_DELETE"


def test_empty_and_malformed_source_fail_closed() -> None:
    with pytest.raises(RetentionPolicyError, match="SOURCE_CHAIN_INVALID:CHAIN_EMPTY"):
        _plan([], keep_last=1, max_remove=1)
    with pytest.raises(RetentionPolicyError, match="SOURCE_CHAIN_INVALID:CHAIN_INVALID"):
        _plan({}, keep_last=1, max_remove=1)


def test_exact_removal_boundary_and_overflow() -> None:
    nodes = _chain(5)
    assert len(_plan(nodes, keep_last=3, max_remove=2)["remove_node_hashes"]) == 2
    with pytest.raises(RetentionPolicyError, match="REMOVE_BOUND_EXCEEDED"):
        _plan(nodes, keep_last=2, max_remove=2)


def test_stale_or_out_of_order_source_fails_closed() -> None:
    with pytest.raises(RetentionPolicyError, match="SOURCE_CHAIN_INVALID:HEAD_STALE"):
        _plan(_chain(2), keep_last=1, max_remove=1, now=NOW + timedelta(seconds=31))
    nodes = _chain(2)
    nodes[1] = _node(1, NOW, previous=nodes[0]["node_hash"])
    with pytest.raises(RetentionPolicyError, match="SOURCE_CHAIN_INVALID:CHRONOLOGY_INVALID"):
        _plan(nodes, keep_last=1, max_remove=1)


def test_tampered_and_partial_plan_fail_closed() -> None:
    nodes = _chain(3)
    plan = _plan(nodes, keep_last=2, max_remove=1)
    tampered = copy.deepcopy(plan)
    tampered["remove_node_hashes"] = []
    with pytest.raises(RetentionPolicyError, match="PLAN_HASH_MISMATCH"):
        _validate_plan(tampered, nodes, now=NOW + timedelta(seconds=3))
    partial = copy.deepcopy(plan)
    partial.pop("operation")
    with pytest.raises(RetentionPolicyError, match="PLAN_FIELDS_INVALID"):
        _validate_plan(partial, nodes, now=NOW + timedelta(seconds=3))


def test_plan_bound_to_exact_source_chain() -> None:
    nodes = _chain(3)
    plan = _plan(nodes, keep_last=2, max_remove=1)
    other = _chain(3, payload_offset=20)
    with pytest.raises(RetentionPolicyError, match="PLAN_SOURCE_MISMATCH"):
        _validate_plan(plan, other, now=NOW + timedelta(seconds=3))


def test_planner_does_not_mutate_or_expose_delete_surface() -> None:
    nodes = _chain(3)
    original = copy.deepcopy(nodes)
    _plan(nodes, keep_last=2, max_remove=1)
    assert nodes == original
    assert "delete" not in dir(plan_retention)
    assert "unlink" not in dir(plan_retention)
    assert "commit" not in dir(plan_retention)


def _plan(nodes, *, keep_last: int, max_remove: int, now: datetime | None = None):
    return plan_retention(
        nodes,
        keep_last=keep_last,
        max_remove=max_remove,
        now=now or NOW + timedelta(seconds=5),
        max_head_age_seconds=30,
        max_nodes=8,
    )


def _validate_plan(plan: dict, nodes, *, now: datetime) -> None:
    validate_retention_plan(
        plan,
        nodes,
        now=now,
        max_head_age_seconds=30,
        max_nodes=8,
    )


def _chain(count: int, *, payload_offset: int = 0) -> list[dict]:
    nodes = []
    previous = None
    for ordinal in range(count):
        node = _node(
            ordinal,
            NOW + timedelta(seconds=ordinal),
            previous=previous,
            payload=f"{ordinal + payload_offset + 1:064x}",
        )
        nodes.append(node)
        previous = node["node_hash"]
    return nodes


def _node(
    ordinal: int,
    generated_at: datetime,
    *,
    previous: str | None,
    payload: str | None = None,
) -> dict:
    return build_chain_node(
        ordinal=ordinal,
        generated_at=generated_at,
        source="paper_pnl",
        source_identity_hash="a" * 64,
        sequence=10 + ordinal,
        payload_hash=payload or f"{ordinal + 1:064x}",
        previous_node_hash=previous,
    )
