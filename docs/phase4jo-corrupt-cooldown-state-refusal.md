# Phase 4JO — Corrupt cooldown-state refusal

Phase 4JO requires positive proof that the Windows-side cooldown artifact exists, ends with a complete record, parses as valid JSON and schema, has valid record hashes and hash-chain linkage, and is durably persisted. Each failed property produces a deterministic refusal reason.

Missing, partial, corrupt, non-durable, malformed, or tampered state fails closed. The evaluator never repairs, truncates, rewrites, skips invalid records, or assumes that missing state means empty restart history.

`ACCEPTED` certifies artifact integrity only and grants no restart, service-control, or execution authority. The implementation is read-only and has no filesystem mutation, process, notification, database, or host-control surface.
