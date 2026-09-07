# Phase 4CK — Snapshot Serialization Optimization

Phase 4CK measures a compact canonical JSON representation against an indented baseline using supplied synthetic snapshot artifacts. An optimization receives credit only after parsing the compact bytes produces the identical Python value and the same canonical semantic hash.

The report records baseline bytes, compact bytes, bytes saved, snapshot count, semantic hash, source hash, and report hash. Input validation bounds the batch to 10,000 snapshots and validates exact snapshot fields, unique identifiers, nonnegative sequences, price bounds, and positive quantities. Canonical JSON sorts object keys, removes insignificant whitespace, preserves Unicode, and is deterministic.

This is measurement only: `optimization_applied_to_runtime=false`. The command has no database, network, exchange, service-control, or production-writer capability and atomically publishes only the requested report.
