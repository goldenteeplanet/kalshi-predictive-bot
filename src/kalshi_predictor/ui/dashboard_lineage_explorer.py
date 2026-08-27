from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

EXPLORER_SCHEMA_VERSION = "phase4gs-dashboard-lineage-explorer-v1"
ExplorerStatus = Literal["READY", "INCOMPLETE", "STALE"]


class DashboardLineageExplorerError(ValueError):
    """Stable fail-closed dashboard-lineage explorer error."""


@dataclass(frozen=True)
class DashboardLineageNode:
    node_id: str
    node_kind: str
    artifact_schema_version: str
    artifact_hash: str
    source_identity_hash: str
    source_watermark: str
    sequence: int
    evidence_age_seconds: int
    available: bool
    node_hash: str


@dataclass(frozen=True)
class DashboardLineageEdge:
    parent_id: str
    child_id: str
    relationship: str
    edge_hash: str


@dataclass(frozen=True)
class DashboardLineageExplorer:
    status: ExplorerStatus
    reasons: tuple[str, ...]
    root_node_id: str
    source_identity_hash: str
    source_watermark: str
    node_count: int
    edge_count: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    traversal_order: tuple[str, ...]
    nodes_hash: str
    edges_hash: str
    explorer_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_lineage_node(
    *,
    node_id: str,
    node_kind: str,
    artifact_schema_version: str,
    artifact_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    sequence: int,
    evidence_age_seconds: int,
    available: bool,
) -> DashboardLineageNode:
    unsigned = {
        "node_id": node_id,
        "node_kind": node_kind,
        "artifact_schema_version": artifact_schema_version,
        "artifact_hash": artifact_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "sequence": sequence,
        "evidence_age_seconds": evidence_age_seconds,
        "available": available,
    }
    _validate_node_fields(unsigned)
    return DashboardLineageNode(**unsigned, node_hash=_hash(unsigned))


def make_dashboard_lineage_edge(
    *, parent_id: str, child_id: str, relationship: str
) -> DashboardLineageEdge:
    unsigned = {
        "parent_id": parent_id,
        "child_id": child_id,
        "relationship": relationship,
    }
    _validate_edge_fields(unsigned)
    return DashboardLineageEdge(**unsigned, edge_hash=_hash(unsigned))


def build_dashboard_lineage_explorer(
    *,
    nodes: Sequence[Any],
    edges: Sequence[Any],
    root_node_id: str,
    max_nodes: int = 64,
    max_edges: int = 128,
    max_evidence_age_seconds: int = 300,
) -> DashboardLineageExplorer:
    for value in (max_nodes, max_edges):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardLineageExplorerError("EXPLORER_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardLineageExplorerError("EXPLORER_BOUND_INVALID")
    if not nodes:
        raise DashboardLineageExplorerError("NODES_EMPTY")
    if len(nodes) > max_nodes:
        raise DashboardLineageExplorerError("NODE_BOUND_EXCEEDED")
    if len(edges) > max_edges:
        raise DashboardLineageExplorerError("EDGE_BOUND_EXCEEDED")

    valid_nodes = [_validated_node(item) for item in nodes]
    valid_edges = [_validated_edge(item) for item in edges]
    node_ids = [item.node_id for item in valid_nodes]
    if len(set(node_ids)) != len(node_ids):
        raise DashboardLineageExplorerError("NODE_ID_DUPLICATE")
    if root_node_id not in node_ids:
        raise DashboardLineageExplorerError("ROOT_NODE_MISSING")
    edge_keys = [(item.parent_id, item.child_id, item.relationship) for item in valid_edges]
    if len(set(edge_keys)) != len(edge_keys):
        raise DashboardLineageExplorerError("EDGE_DUPLICATE")
    node_by_id = {item.node_id: item for item in valid_nodes}
    if any(
        item.parent_id not in node_by_id or item.child_id not in node_by_id for item in valid_edges
    ):
        raise DashboardLineageExplorerError("EDGE_ENDPOINT_MISSING")
    if any(item.parent_id == item.child_id for item in valid_edges):
        raise DashboardLineageExplorerError("LINEAGE_SELF_CYCLE")
    if any(
        node_by_id[item.parent_id].sequence >= node_by_id[item.child_id].sequence
        for item in valid_edges
    ):
        raise DashboardLineageExplorerError("LINEAGE_SEQUENCE_INVALID")

    identities = {item.source_identity_hash for item in valid_nodes}
    watermarks = {item.source_watermark for item in valid_nodes}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardLineageExplorerError("NODE_LINEAGE_MIXED")
    traversal = _traversal(root_node_id, node_ids, valid_edges)
    if set(traversal) != set(node_ids):
        raise DashboardLineageExplorerError("LINEAGE_DISCONNECTED")

    ordered_nodes = sorted(valid_nodes, key=lambda item: (item.sequence, item.node_id))
    ordered_edges = sorted(
        valid_edges, key=lambda item: (item.parent_id, item.child_id, item.relationship)
    )
    observed_age = max(item.evidence_age_seconds for item in ordered_nodes)
    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: ExplorerStatus = "STALE"
        reasons.append("LINEAGE_EVIDENCE_STALE")
    else:
        unavailable = sorted(item.node_id for item in ordered_nodes if not item.available)
        reasons.extend(f"NODE_UNAVAILABLE:{node_id}" for node_id in unavailable)
        status = "INCOMPLETE" if reasons else "READY"

    nodes_hash = _hash([asdict(item) for item in ordered_nodes])
    edges_hash = _hash([asdict(item) for item in ordered_edges])
    identity = ordered_nodes[0].source_identity_hash
    watermark = ordered_nodes[0].source_watermark
    unsigned = {
        "schema_version": EXPLORER_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "root_node_id": root_node_id,
        "source_identity_hash": identity,
        "source_watermark": watermark,
        "node_count": len(ordered_nodes),
        "edge_count": len(ordered_edges),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "traversal_order": list(traversal),
        "nodes_hash": nodes_hash,
        "edges_hash": edges_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardLineageExplorer(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        root_node_id=root_node_id,
        source_identity_hash=identity,
        source_watermark=watermark,
        node_count=len(ordered_nodes),
        edge_count=len(ordered_edges),
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        traversal_order=tuple(traversal),
        nodes_hash=nodes_hash,
        edges_hash=edges_hash,
        explorer_hash=_hash(unsigned),
    )


def validate_dashboard_lineage_explorer(explorer: Any) -> None:
    if not isinstance(explorer, DashboardLineageExplorer):
        raise DashboardLineageExplorerError("EXPLORER_RESULT_TYPE_INVALID")
    if explorer.read_only is not True or explorer.execution_authorized is not False:
        raise DashboardLineageExplorerError("EXPLORER_SAFETY_BOUNDARY_INVALID")
    if explorer.status == "READY" and explorer.reasons:
        raise DashboardLineageExplorerError("READY_STATE_INVALID")
    if explorer.status in {"INCOMPLETE", "STALE"} and not explorer.reasons:
        raise DashboardLineageExplorerError("NON_READY_REASONS_MISSING")
    unsigned = asdict(explorer)
    unsigned.pop("explorer_hash")
    unsigned["schema_version"] = EXPLORER_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["traversal_order"] = list(unsigned["traversal_order"])
    if explorer.explorer_hash != _hash(unsigned):
        raise DashboardLineageExplorerError("EXPLORER_HASH_MISMATCH")


def _traversal(root: str, node_ids: list[str], edges: list[DashboardLineageEdge]) -> list[str]:
    children = {node_id: [] for node_id in node_ids}
    for edge in edges:
        children[edge.parent_id].append(edge.child_id)
    for values in children.values():
        values.sort()
    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise DashboardLineageExplorerError("LINEAGE_CYCLE")
        if node_id in visited:
            return
        visiting.add(node_id)
        result.append(node_id)
        for child_id in children[node_id]:
            visit(child_id)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(root)
    return result


def _validated_node(value: Any) -> DashboardLineageNode:
    if not isinstance(value, DashboardLineageNode):
        raise DashboardLineageExplorerError("NODE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("node_hash")
    _validate_node_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardLineageExplorerError("NODE_HASH_MISMATCH")
    return value


def _validated_edge(value: Any) -> DashboardLineageEdge:
    if not isinstance(value, DashboardLineageEdge):
        raise DashboardLineageExplorerError("EDGE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("edge_hash")
    _validate_edge_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardLineageExplorerError("EDGE_HASH_MISMATCH")
    return value


def _validate_node_fields(payload: dict[str, Any]) -> None:
    for key in (
        "node_id",
        "node_kind",
        "artifact_schema_version",
        "artifact_hash",
        "source_identity_hash",
        "source_watermark",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardLineageExplorerError("NODE_FIELD_INVALID")
    for key in ("sequence", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardLineageExplorerError("NODE_FIELD_INVALID")
    if not isinstance(payload["available"], bool):
        raise DashboardLineageExplorerError("NODE_FIELD_INVALID")


def _validate_edge_fields(payload: dict[str, Any]) -> None:
    for key in ("parent_id", "child_id", "relationship"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardLineageExplorerError("EDGE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
