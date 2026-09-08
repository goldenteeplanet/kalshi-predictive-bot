# Phase 4JM — Crash-boundary simulation

Phase 4JM models crashes at six persistence boundaries: before and after intent persistence, after warning recording, before and after mock execution, and before post-boot verification. Each boundary has an exact expected tuple for intent persistence, pending post-boot state, and mock invocation count.

State mismatches yield `FAIL`; unknown boundaries or non-fixture inputs are `TAMPERED`; partial evidence is `INCOMPLETE`. Once an intent or mock invocation may exist, reconciliation is mandatory. Automatic replay is prohibited at every boundary to prevent duplicate restart attempts.

The simulator is deterministic, read-only, and fixture-only. It grants no restart, process-spawn, service-control, or execution authority and contains no filesystem, database, notification, process, or host-control surface.
