# Phase 4GS — Dashboard Lineage Explorer

## Outcome and measured evidence

Phase 4GS adds an artifact-only lineage DAG explorer for dashboard evidence. It validates node and edge
hashes, chronological direction, endpoints, reachability, freshness, and availability before emitting a
stable traversal. Focused tests cover deterministic order, empty input, missing endpoints, node bounds,
exact freshness, unavailable nodes, disconnected graphs, malformed sequences, cycles, mixed lineage,
tampering, and immutable safety boundaries.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gs-dashboard-lineage-explorer-v1`.
- Nodes bind ID, kind, schema/artifact hashes, source identity/watermark, sequence, age, and availability.
- Edges bind parent, child, and relationship. Every edge must point from a lower to a higher sequence;
  every node must be reachable from the declared root.
- Default limits are 64 nodes and 128 edges. Evidence exactly 300 seconds old remains eligible; one
  second older makes the explorer `STALE`. Unavailable nodes yield `INCOMPLETE` with stable reasons.
- Canonical SHA-256 protects nodes, edges, traversal, and output.

## Safety, rejected alternatives, rollback, and next dependency

The explorer performs no database traversal, file discovery, HTTP request, write, or service control and
always emits `read_only=true` and `execution_authorized=false`. Recursive live database exploration and
automatic repair of missing links were rejected because they add unbounded load or mutation authority.

Rollback is deletion of this module, focused test, and report. Phase 4GT should use validated lineage
nodes as provenance anchors for a deterministic settlement timeline.
