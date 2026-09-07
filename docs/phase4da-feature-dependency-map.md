# Phase 4DA — Forecast Feature Dependency Map

Phase 4DA models forecast features and upstream evidence as a deterministic directed acyclic graph. Every evidence node carries an exact source, schema hash, artifact hash, and freshness limit. Every feature declares direct dependencies, computation cost, and invalidation triggers.

The resolver emits a stable topological order, transitive upstream evidence, transitive feature closure, the strictest upstream freshness limit, and cumulative computation cost without double-counting shared dependencies. Cycles, missing nodes, duplicate dependencies, malformed hashes, and feature/evidence identifier collisions fail closed.

All features require evidence-hash, freshness-expiry, and schema-change invalidation. Features depending on other features additionally require feature-dependency invalidation. Inputs and reports are deterministic, bounded, hash-protected, and atomically published. The tool creates no forecast or production record and has no database, network, exchange, service-control, or trading capability.
