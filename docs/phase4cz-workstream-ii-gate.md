# Phase 4CZ — Workstream II Final Gate

Phase 4CZ requires all 25 phases from 4CA through 4CY exactly once. Every phase row is independently hash-protected and must prove complete lineage, deterministic replay, resource bounds, and safety. The gate also requires a positive cumulative test count, nonnegative expected platform skips, and a passing Ruff status.

Changed paths are restricted to Workstream II local phase scripts, tests, documentation, and the Phase 4 roadmap. Any production source, collector, deployment, parent traversal, absolute path, or backslash-form path refuses advancement. This allows the gate to distinguish this workstream's files from pre-existing unrelated worktree changes.

A passing report authorizes advancement to the next development workstream only; trading execution remains false. The gate is deterministic, canonically hash-protected, atomically published, and has no database, network, exchange, service-control, order-creation, or production-writer capability.
