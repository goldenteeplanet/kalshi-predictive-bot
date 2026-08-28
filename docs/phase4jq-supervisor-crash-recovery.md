# Phase 4JQ — Supervisor crash recovery

Phase 4JQ classifies recovery after a supervisor owner disappears. A live owner is retained. A dead owner requires operator reconciliation of the lock and, when present, the durable restart intent and pending post-boot marker. Unknown owner liveness or untrusted lock evidence is denied.

Pending post-boot verification without a durable intent is contradictory and treated as tampered. No state permits automatic replay, lock stealing, or lock release; this phase generates a reconciliation disposition only.

The evaluator is deterministic and read-only and grants no restart, service-control, or execution authority. It contains no lock mutation, filesystem write, database, notification, process, or host-control surface.
