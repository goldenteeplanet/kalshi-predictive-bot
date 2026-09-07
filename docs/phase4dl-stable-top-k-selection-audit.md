# Phase 4DL — Stable Top-K Selection Audit

Phase 4DL evaluates a bounded insertion-based top-K path against a complete deterministic
sort. Both paths use exact Decimal score descending and `candidate_id` ascending tie
semantics. Selection at a tied K boundary therefore remains total and reproducible.

The report records deterministic comparison work and its `candidate_count × k` bound;
wall-clock timing is not used as a CI gate. Every bounded result must equal the full-sort
result item-for-item and by canonical hash.

Invalid K values, non-finite scores, duplicate candidates, malformed contracts, and input
tampering fail closed. Atomic publication creates no ranking and the module has no
database, network, exchange, service-control, or trading capability.
