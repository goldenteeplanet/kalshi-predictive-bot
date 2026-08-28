# Phase 4JN — Power-loss state simulation

Phase 4JN models power loss before append, during append, after append but before `fsync`, and after `fsync`. Each boundary has an exact expected tuple for bytes present, record completeness, and durability.

Truncated state is `REFUSE_CORRUPT`; complete but non-durable state is `REFUSE_NOT_DURABLE`; a durable record requires reconciliation; no record means no intent. Unknown boundaries, non-fixture inputs, partial evidence, mismatches, malformed data, and tampering fail closed. Automatic replay is prohibited for every disposition.

The simulator is deterministic, read-only, and fixture-only. It grants no restart, service-control, or execution authority and performs no filesystem, database, notification, process, or host-control operation.
