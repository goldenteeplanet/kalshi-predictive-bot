# Phase 4EO — Fill Simulation Latency Audit

Phase 4EO benchmarks fixed synthetic top-of-book, queue-ahead, and pro-rata fill models.
Baseline and optimized paths produce identical integer fill quantities. Pro-rata uses exact
integer basis-point arithmetic, and queue-ahead capacity cannot fall below zero.

Latency is represented by deterministic setup and evaluation work units. The optimized
model reuses setup for the same model and book snapshot; wall-clock timing is never a
correctness gate. Invalid models, unused parameters, malformed quantities or hashes,
duplicate scenarios, schema drift, and artifact tampering fail closed.

All fills are synthetic artifact values. The audit modifies and creates zero real paper
fills, performs zero database writes, has no connected-system capability, and publishes
atomically.
