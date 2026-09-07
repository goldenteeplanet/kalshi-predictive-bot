# Phase 4EP — Partial-Fill State Machine

Phase 4EP replays synthetic fill, expiration, cancellation, ambiguity, and rollback events.
Fill quantities are conserved exactly across OPEN, PARTIAL, and COMPLETE states. EXPIRED,
CANCELLED, and AMBIGUOUS are terminal. A rollback must target the most recent active fill
and can safely reopen a COMPLETE or PARTIAL state.

Event sequences must be unique and contiguous, and replaying the normalized stream must
produce exactly the same transitions, quantities, and final state. Overfills, events after
terminal states, invalid rollback targets, malformed event-specific fields, schema drift,
and artifact tampering fail closed.

The machine is artifact-only. It modifies and creates zero real paper fills, performs zero
database writes, has no connected execution capability, and publishes atomically.
