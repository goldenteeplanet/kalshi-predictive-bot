# Phase 4CS — Data-Stage Resource Bounds

Phase 4CS evaluates supplied stage measurements against explicit maxima for pages, markets, snapshots, serialized bytes, peak memory, and elapsed time. Pages, markets, snapshots, and bytes accumulate across stages; peak memory and elapsed time are monotonic gauges.

Every boundary is inclusive. Equality passes, while a one-unit overage refuses at that exact stage. The first exceeded metric follows a fixed metric order, and all subsequent stages are marked `NOT_EVALUATED_AFTER_REFUSAL`. Decreasing memory or elapsed gauges is rejected as malformed evidence rather than interpreted as recovery.

Inputs and reports are exact-schema, bounded, deterministic, canonically hash-protected, and atomically published. This is an artifact-only ledger with no data acquisition, database, exchange, service-control, or production-writer capability; it never authorizes execution.
