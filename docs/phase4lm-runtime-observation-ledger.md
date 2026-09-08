# Phase 4LM — Runtime Observation Timeline and Recurrence Ledger

## Outcome

Phase 4LM converts hash-valid Phase 4LL classifications into a deterministic, chronological,
deduplicated, hash-chained observation ledger. Each record binds snapshot and classification
provenance and records both consecutive and rolling recurrence counts.

## Failure behavior

Malformed timestamps, schemas, event kinds, or classification hashes cause a `REFUSE` verdict and
are excluded from the ledger. Duplicate observations do not inflate recurrence counts. The model is
append-only and does not mutate a database or runtime.

## Safety and removal

The implementation has no network, database-write, service-control, WSL-control, or trading
capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LN — Alert deduplication, cooldown, and acknowledgement-state contract.
