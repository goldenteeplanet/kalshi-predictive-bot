# Phase 4JR — Replay idempotency verification

Phase 4JR compares first-pass and replay results for canonical recovery scenarios. A passing case requires identical result hashes, identical state hashes, and identical cumulative effect counts. The first pass may add at most one bounded fixture effect; replay may add none.

Output, state, or effect divergence yields `FAIL`; empty or partial sets are `INCOMPLETE`; duplicate scenarios are `TAMPERED`. Inputs, canonical ordering, case sets, and decisions are bounded and integrity-bound.

Passing evidence permits no additional effects and grants no restart, service-control, or execution authority. The verifier is read-only and contains no filesystem, database, notification, process, or host-control surface.
