# Phase 4DX — Forecast Pipeline Differential Replay

Phase 4DX compares supplied legacy and optimized pipeline outputs over a required corpus
covering normal, boundary, tie, stale, and refusal behavior. Success/refusal shape, result
ordering, values, and reason ordering are all logically significant and must match exactly.

Per-fixture canonical hashes and equality results are recorded, along with deterministic
legacy and optimized work units. Work savings never excuse a logical difference, and
wall-clock timing is not used as a gate. Missing corpus categories, malformed outcomes,
duplicate fixtures, invalid work units, and tampering fail closed.

This is artifact-only replay. Atomic publication creates no forecast and the module has no
database, network, exchange, service-control, or trading capability.
