from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.phase4cd.read_model_chain import (
    ReadModelChainError,
    build_chain_node,
    validate_chain,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def test_valid_chain_reports_progress() -> None:
    nodes = _chain((10, 10, 13))
    result = _validate(nodes, now=NOW + timedelta(seconds=3))
    assert result.node_count == 3
    assert result.first_sequence == 10
    assert result.last_sequence == 13
    assert result.progress == 3


def test_empty_and_malformed_chain_fail_closed() -> None:
    with pytest.raises(ReadModelChainError, match="CHAIN_EMPTY"):
        _validate([], now=NOW)
    with pytest.raises(ReadModelChainError, match="CHAIN_INVALID"):
        _validate({}, now=NOW)


def test_exact_node_and_staleness_boundaries() -> None:
    nodes = _chain((10, 11))
    assert _validate(nodes, now=NOW + timedelta(seconds=2), max_nodes=2).node_count == 2
    with pytest.raises(ReadModelChainError, match="CHAIN_NODE_BOUND_EXCEEDED"):
        _validate(nodes, now=NOW + timedelta(seconds=2), max_nodes=1)
    with pytest.raises(ReadModelChainError, match="HEAD_STALE"):
        _validate(nodes, now=NOW + timedelta(seconds=31))


def test_tampered_node_fails_closed() -> None:
    nodes = _chain((10, 11))
    nodes[1]["sequence"] = 12
    with pytest.raises(ReadModelChainError, match="NODE_HASH_MISMATCH"):
        _validate(nodes, now=NOW + timedelta(seconds=2))


def test_missing_link_and_ordinal_fail_closed() -> None:
    nodes = _chain((10, 11))
    nodes[1] = _node(1, 11, NOW + timedelta(seconds=1), previous=None)
    with pytest.raises(ReadModelChainError, match="CHAIN_LINK_INVALID"):
        _validate(nodes, now=NOW + timedelta(seconds=2))
    nodes = _chain((10, 11))
    nodes[1]["ordinal"] = 2
    with pytest.raises(ReadModelChainError, match="NODE_HASH_MISMATCH"):
        _validate(nodes, now=NOW + timedelta(seconds=2))


def test_duplicate_payload_and_sequence_regression_fail_closed() -> None:
    nodes = _chain((10, 11))
    duplicate = _node(
        1,
        11,
        NOW + timedelta(seconds=1),
        previous=nodes[0]["node_hash"],
        payload="0" * 64,
    )
    nodes[0] = _node(0, 10, NOW, previous=None, payload="0" * 64)
    nodes[1] = duplicate
    with pytest.raises(ReadModelChainError, match="PAYLOAD_DUPLICATE"):
        _validate(nodes, now=NOW + timedelta(seconds=2))
    with pytest.raises(ReadModelChainError, match="SEQUENCE_REGRESSED"):
        _validate(_chain((10, 9)), now=NOW + timedelta(seconds=2))


def test_chronology_and_source_identity_fail_closed() -> None:
    nodes = _chain((10, 11))
    nodes[1] = _node(1, 11, NOW, previous=nodes[0]["node_hash"])
    with pytest.raises(ReadModelChainError, match="CHRONOLOGY_INVALID"):
        _validate(nodes, now=NOW + timedelta(seconds=1))
    nodes = _chain((10, 11))
    nodes[1] = _node(
        1,
        11,
        NOW + timedelta(seconds=1),
        previous=nodes[0]["node_hash"],
        identity="b" * 64,
    )
    with pytest.raises(ReadModelChainError, match="SOURCE_IDENTITY_CHANGED"):
        _validate(nodes, now=NOW + timedelta(seconds=2))


def test_partial_node_and_future_head_fail_closed() -> None:
    nodes = _chain((10,))
    nodes[0].pop("payload_hash")
    with pytest.raises(ReadModelChainError, match="NODE_FIELDS_INVALID"):
        _validate(nodes, now=NOW)
    with pytest.raises(ReadModelChainError, match="HEAD_FUTURE_DATED"):
        _validate(_chain((10,)), now=NOW - timedelta(seconds=1))


def test_validation_does_not_mutate_input_or_expose_writer() -> None:
    nodes = _chain((10, 11))
    original = copy.deepcopy(nodes)
    _validate(nodes, now=NOW + timedelta(seconds=2))
    assert nodes == original
    assert "execute" not in dir(validate_chain)
    assert "commit" not in dir(validate_chain)


def _validate(nodes, *, now: datetime, max_nodes: int = 8):
    return validate_chain(nodes, now=now, max_head_age_seconds=30, max_nodes=max_nodes)


def _chain(sequences: tuple[int, ...]) -> list[dict]:
    nodes = []
    previous = None
    for ordinal, sequence in enumerate(sequences):
        node = _node(ordinal, sequence, NOW + timedelta(seconds=ordinal), previous=previous)
        nodes.append(node)
        previous = node["node_hash"]
    return nodes


def _node(
    ordinal: int,
    sequence: int,
    generated_at: datetime,
    *,
    previous: str | None,
    identity: str = "a" * 64,
    payload: str | None = None,
) -> dict:
    return build_chain_node(
        ordinal=ordinal,
        generated_at=generated_at,
        source="paper_pnl",
        source_identity_hash=identity,
        sequence=sequence,
        payload_hash=payload or f"{ordinal + 1:064x}",
        previous_node_hash=previous,
    )
