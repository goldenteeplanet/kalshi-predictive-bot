from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

CHAIN_SCHEMA_VERSION = "phase4fq-read-model-chain-v1"


class ReadModelChainError(ValueError):
    """Stable fail-closed chain validation error."""


@dataclass(frozen=True)
class ReadModelChainResult:
    node_count: int
    genesis_hash: str
    head_hash: str
    source: str
    source_identity_hash: str
    first_sequence: int
    last_sequence: int
    progress: int
    head_age_seconds: float


def build_chain_node(
    *,
    ordinal: int,
    generated_at: datetime,
    source: str,
    source_identity_hash: str,
    sequence: int,
    payload_hash: str,
    previous_node_hash: str | None,
) -> dict[str, Any]:
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ReadModelChainError("ORDINAL_INVALID")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ReadModelChainError("SEQUENCE_INVALID")
    if not source or ":" in source:
        raise ReadModelChainError("SOURCE_INVALID")
    _require_aware(generated_at, "GENERATED_AT_NAIVE")
    _require_hash(source_identity_hash, "SOURCE_IDENTITY_HASH_INVALID")
    _require_hash(payload_hash, "PAYLOAD_HASH_INVALID")
    if previous_node_hash is not None:
        _require_hash(previous_node_hash, "PREVIOUS_NODE_HASH_INVALID")
    payload: dict[str, Any] = {
        "schema_version": CHAIN_SCHEMA_VERSION,
        "ordinal": ordinal,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "source": source,
        "source_identity_hash": source_identity_hash,
        "sequence": sequence,
        "payload_hash": payload_hash,
        "previous_node_hash": previous_node_hash,
    }
    payload["node_hash"] = _hash(payload)
    return payload


def validate_chain(
    nodes: Any,
    *,
    now: datetime,
    max_head_age_seconds: float,
    max_nodes: int,
) -> ReadModelChainResult:
    _require_aware(now, "NOW_NAIVE")
    if max_head_age_seconds <= 0 or max_nodes <= 0:
        raise ReadModelChainError("BOUND_INVALID")
    if not isinstance(nodes, list):
        raise ReadModelChainError("CHAIN_INVALID")
    if not nodes:
        raise ReadModelChainError("CHAIN_EMPTY")
    if len(nodes) > max_nodes:
        raise ReadModelChainError("CHAIN_NODE_BOUND_EXCEEDED")

    validated = [_validate_node(node) for node in nodes]
    payload_hashes: set[str] = set()
    previous: dict[str, Any] | None = None
    for expected_ordinal, node in enumerate(validated):
        if node["ordinal"] != expected_ordinal:
            raise ReadModelChainError("ORDINAL_DISCONTINUITY")
        if node["payload_hash"] in payload_hashes:
            raise ReadModelChainError("PAYLOAD_DUPLICATE")
        payload_hashes.add(node["payload_hash"])
        if previous is None:
            if node["previous_node_hash"] is not None:
                raise ReadModelChainError("GENESIS_LINK_INVALID")
        else:
            if node["previous_node_hash"] != previous["node_hash"]:
                raise ReadModelChainError("CHAIN_LINK_INVALID")
            if _timestamp(node) <= _timestamp(previous):
                raise ReadModelChainError("CHRONOLOGY_INVALID")
            if node["source"] != previous["source"]:
                raise ReadModelChainError("SOURCE_CHANGED")
            if node["source_identity_hash"] != previous["source_identity_hash"]:
                raise ReadModelChainError("SOURCE_IDENTITY_CHANGED")
            if node["sequence"] < previous["sequence"]:
                raise ReadModelChainError("SEQUENCE_REGRESSED")
        previous = node

    genesis = validated[0]
    head = validated[-1]
    head_age_seconds = (now.astimezone(UTC) - _timestamp(head)).total_seconds()
    if head_age_seconds < 0:
        raise ReadModelChainError("HEAD_FUTURE_DATED")
    if head_age_seconds >= max_head_age_seconds:
        raise ReadModelChainError("HEAD_STALE")
    return ReadModelChainResult(
        node_count=len(validated),
        genesis_hash=genesis["node_hash"],
        head_hash=head["node_hash"],
        source=head["source"],
        source_identity_hash=head["source_identity_hash"],
        first_sequence=genesis["sequence"],
        last_sequence=head["sequence"],
        progress=head["sequence"] - genesis["sequence"],
        head_age_seconds=head_age_seconds,
    )


def _validate_node(node: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "ordinal",
        "generated_at",
        "source",
        "source_identity_hash",
        "sequence",
        "payload_hash",
        "previous_node_hash",
        "node_hash",
    }
    if not isinstance(node, dict) or set(node) != required:
        raise ReadModelChainError("NODE_FIELDS_INVALID")
    if node["schema_version"] != CHAIN_SCHEMA_VERSION:
        raise ReadModelChainError("NODE_SCHEMA_UNSUPPORTED")
    if not isinstance(node["ordinal"], int) or isinstance(node["ordinal"], bool):
        raise ReadModelChainError("ORDINAL_INVALID")
    if not isinstance(node["sequence"], int) or isinstance(node["sequence"], bool):
        raise ReadModelChainError("SEQUENCE_INVALID")
    if not isinstance(node["source"], str) or not node["source"] or ":" in node["source"]:
        raise ReadModelChainError("SOURCE_INVALID")
    _timestamp(node)
    _require_hash(node["source_identity_hash"], "SOURCE_IDENTITY_HASH_INVALID")
    _require_hash(node["payload_hash"], "PAYLOAD_HASH_INVALID")
    if node["previous_node_hash"] is not None:
        _require_hash(node["previous_node_hash"], "PREVIOUS_NODE_HASH_INVALID")
    unhashed = {key: value for key, value in node.items() if key != "node_hash"}
    if node["node_hash"] != _hash(unhashed):
        raise ReadModelChainError("NODE_HASH_MISMATCH")
    return node


def _timestamp(node: dict[str, Any]) -> datetime:
    value = node["generated_at"]
    if not isinstance(value, str):
        raise ReadModelChainError("GENERATED_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReadModelChainError("GENERATED_AT_INVALID") from exc
    _require_aware(parsed, "GENERATED_AT_NAIVE")
    return parsed.astimezone(UTC)


def _require_aware(value: datetime, reason: str) -> None:
    if value.tzinfo is None:
        raise ReadModelChainError(reason)


def _require_hash(value: Any, reason: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ReadModelChainError(reason)
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ReadModelChainError(reason) from exc


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
