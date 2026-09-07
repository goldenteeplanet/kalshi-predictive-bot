"""Validate and analyze the guarded time-to-trade dependency DAG."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bq.dependency-dag-input.v1"
SCHEMA = "phase4bq.critical-path-dag.v1"
ANALYSIS_SCHEMA = "phase4bq.serialization-analysis.v1"
NODES = (
    "COLLECTION",
    "SNAPSHOT",
    "FORECAST",
    "RANKING",
    "POSITION_SIZING",
    "ADVANCED_RISK",
    "APPROVAL",
    "PAPER_ROUTING",
    "OBSERVABILITY",
)
REQUIRED_EDGES = tuple(zip(NODES[:-1], NODES[1:], strict=True))
MAX_NODE_DURATION_MS = 3_600_000


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _reachable(start: str, target: str, adjacency: dict[str, set[str]]) -> bool:
    stack, seen = [start], set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(sorted(adjacency[node], reverse=True))
    return False


def build(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BQ_INPUT_SCHEMA_OR_HASH_INVALID")
    nodes, edges = payload.get("nodes"), payload.get("edges")
    if not isinstance(nodes, list) or [
        row.get("node") for row in nodes if isinstance(row, dict)
    ] != list(NODES):
        raise ValueError("PHASE4BQ_NODE_COVERAGE_OR_ORDER_INVALID")
    durations: dict[str, int] = {}
    for row in nodes:
        if set(row) != {"node", "expected_duration_ms", "evidence_hash"}:
            raise ValueError("PHASE4BQ_NODE_FIELDS_INVALID")
        duration = row["expected_duration_ms"]
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not 0 <= duration <= MAX_NODE_DURATION_MS
        ):
            raise ValueError("PHASE4BQ_NODE_DURATION_INVALID")
        if not isinstance(row["evidence_hash"], str) or len(row["evidence_hash"]) != 64:
            raise ValueError("PHASE4BQ_NODE_EVIDENCE_INVALID")
        durations[row["node"]] = duration
    if not isinstance(edges, list):
        raise ValueError("PHASE4BQ_EDGES_INVALID")
    normalized_edges: list[tuple[str, str]] = []
    for row in edges:
        if not isinstance(row, dict) or set(row) != {"source", "target", "reason_hash"}:
            raise ValueError("PHASE4BQ_EDGE_FIELDS_INVALID")
        edge = (row["source"], row["target"])
        if edge[0] not in NODES or edge[1] not in NODES or edge[0] == edge[1]:
            raise ValueError("PHASE4BQ_EDGE_ENDPOINT_INVALID")
        if not isinstance(row["reason_hash"], str) or len(row["reason_hash"]) != 64:
            raise ValueError("PHASE4BQ_EDGE_EVIDENCE_INVALID")
        normalized_edges.append(edge)
    if len(normalized_edges) != len(set(normalized_edges)):
        raise ValueError("PHASE4BQ_DUPLICATE_EDGE")
    if not set(REQUIRED_EDGES).issubset(normalized_edges):
        raise ValueError("PHASE4BQ_REQUIRED_EDGE_MISSING")
    adjacency = {node: set() for node in NODES}
    indegree = {node: 0 for node in NODES}
    for source, target in normalized_edges:
        adjacency[source].add(target)
        indegree[target] += 1
    ready = [node for node in NODES if indegree[node] == 0]
    topological: list[str] = []
    while ready:
        ready.sort(key=NODES.index)
        node = ready.pop(0)
        topological.append(node)
        for target in sorted(adjacency[node], key=NODES.index):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(topological) != len(NODES):
        raise ValueError("PHASE4BQ_CYCLE_DETECTED")
    longest = {node: durations[node] for node in NODES}
    predecessor: dict[str, str | None] = {node: None for node in NODES}
    for source in topological:
        for target in sorted(adjacency[source], key=NODES.index):
            candidate = longest[source] + durations[target]
            if candidate > longest[target]:
                longest[target], predecessor[target] = candidate, source
    terminal = max(NODES, key=lambda node: (longest[node], -NODES.index(node)))
    critical_path: list[str] = []
    cursor: str | None = terminal
    while cursor is not None:
        critical_path.append(cursor)
        cursor = predecessor[cursor]
    critical_path.reverse()
    redundant: list[dict[str, str]] = []
    for source, target in normalized_edges:
        reduced = {node: set(values) for node, values in adjacency.items()}
        reduced[source].remove(target)
        if _reachable(source, target, reduced):
            redundant.append({"source": source, "target": target})
    dag: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BQ",
        "input_hash": payload["artifact_hash"],
        "nodes": nodes,
        "edges": edges,
        "topological_order": topological,
        "critical_path": critical_path,
        "critical_path_expected_duration_ms": longest[terminal],
        "acyclic": True,
        "execution_authorized": False,
    }
    dag["artifact_hash"] = _hash(dag)
    analysis: dict[str, Any] = {
        "schema": ANALYSIS_SCHEMA,
        "phase": "4BQ",
        "dag_hash": dag["artifact_hash"],
        "redundant_serial_edges": sorted(
            redundant, key=lambda row: (NODES.index(row["source"]), NODES.index(row["target"]))
        ),
        "redundant_serial_edge_count": len(redundant),
        "optimization_candidates_only": True,
        "execution_authorized": False,
    }
    analysis["artifact_hash"] = _hash(analysis)
    return dag, analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dag-input", type=Path, required=True)
    parser.add_argument("--dag-output", type=Path, required=True)
    parser.add_argument("--analysis-output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.dag_input.read_text(encoding="utf-8"))
    dag, analysis = build(payload)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.dag_output, args.analysis_output, dag, analysis)
    print(json.dumps(dag, sort_keys=True))


if __name__ == "__main__":
    main()
