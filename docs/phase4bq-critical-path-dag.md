# Phase 4BQ — Critical-Path Dependency Graph

Phase 4BQ validates the exact collection-to-observability node set, mandatory dependencies,
evidence hashes, duration estimates, and graph acyclicity. It deterministically calculates a
topological order and expected-duration critical path.

The companion serialization analysis removes each edge in turn and reports edges whose endpoints
remain connected. These redundant serial edges are optimization candidates only; the artifact does
not modify pipeline scheduling or authorize execution.
