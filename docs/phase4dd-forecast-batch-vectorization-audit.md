# Phase 4DD — Forecast Batch Vectorization Audit

Phase 4DD audits synthetic forecast-score batching against an independent scalar
baseline. Both paths use exact `Decimal` arithmetic for `(probability - market_price) *
weight`; binary floating point is excluded. The audit preserves supplied row order and
records exact inclusive/exclusive batch offsets, including a partial final batch.

Advancement requires byte-identical canonical result hashes and item-for-item equality.
Invalid probabilities, prices, decimals, batch sizes, duplicate identities, malformed
contracts, or hash tampering fail closed. Deterministic operation counts and batch
structure are recorded instead of treating noisy wall-clock timing as proof of safety.

The tool consumes only supplied synthetic fixtures and atomically publishes a
hash-protected report. It has no database, network, forecast creation, exchange,
service-control, or trading capability.
