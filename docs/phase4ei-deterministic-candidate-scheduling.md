# Phase 4EI — Deterministic Candidate Scheduling

Phase 4EI orders candidate risk evaluations by earliest deadline, least remaining evidence
freshness, then candidate identifier. All slack and age calculations use exact UTC integer
milliseconds. Deadline and freshness equality remain schedulable; a one-millisecond expiry,
stale observation, or future observation refuses before scheduling.

The artifact separates scheduled and refused candidates, records every computed timing
value and the explicit ordering contract, and assigns stable one-based positions. Input
order, host time, and wall-clock performance cannot change the result. Invalid timestamps,
hashes, bounds, duplicate identities, schema drift, and tampering fail closed.

Scheduling is descriptive only: no evaluation runs, no capital is reserved, no record or
order is created, and publication is atomic without connected-system access.
