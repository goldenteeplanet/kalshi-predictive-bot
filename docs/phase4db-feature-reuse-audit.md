# Phase 4DB — Feature Reuse Audit

Phase 4DB audits forecast-feature instances for safe cross-forecast content addressing. A content key binds the feature identifier, exact value, computation version, and named upstream artifact hashes. Mapping key order does not weaken identity because canonical hashing is used.

Reuse eligibility requires deterministic computation and either immutable scope or time-bound validity through the inclusive evaluation instant. Forecast-specific, expired, or nondeterministic instances remain ineligible. A reuse group is emitted only when the same content key appears in at least two distinct forecasts; duplicate instances within one forecast do not prove reuse.

The phase performs no content-store write and creates no forecast. Inputs and reports are exact-schema, bounded, deterministic, hash-protected, and atomically published. It has no database, cache, network, exchange, service-control, or trading capability.
