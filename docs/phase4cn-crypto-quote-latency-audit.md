# Phase 4CN — Crypto Quote Source Latency Audit

Phase 4CN audits supplied crypto quote artifacts for freshness, bid/ask coherence, missing-symbol behavior, cross-source temporal alignment, and midpoint agreement. It performs no provider calls.

Per-symbol outcomes distinguish missing quotes, crossed books, stale quotes, excessive timestamp skew, and price divergence. Freshness, skew, and midpoint-spread thresholds are inclusive. Decimal arithmetic avoids binary floating-point price boundary errors, while integer millisecond arithmetic governs time boundaries.

Inputs use an exact hash-protected schema, an explicit expected-symbol list, unique quote identifiers, finite nonnegative prices, UTC timestamps, and bounded quote counts. The output is deterministic, hash-protected, and atomically published. The tool has no network, database, exchange, service-control, or production-writer capability.
