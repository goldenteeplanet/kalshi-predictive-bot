# Phase 4HO — Alert Severity and Deduplication

## Outcome and measured evidence

Phase 4HO deterministically maps allowlisted incident categories to `INFO`, `WARNING`, or `CRITICAL`,
derives a privacy-preserving deduplication key, and returns `EMIT`, `SUPPRESS_DUPLICATE`, `STALE`,
`INCOMPLETE`, or `DENY`. Same-or-lower-severity repeats inside the window are suppressed; a higher
severity, resolved prior alert, or exact window expiry may emit. Emission is only a candidate and does
not authorize delivery or send a notification.

Focused tests cover category severity, deterministic history ordering, suppression, exact window
boundary, escalation, resolution, stale/incomplete/unknown/future candidates, history bounds/duplicate
IDs/future records, candidate/history/result tampering, and forbidden delivery/control surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4ho-alert-severity-deduplication-v1`.
- Default deduplication window: 300 seconds; exact expiry emits while elapsed time below it suppresses.
- Default history bound: 256 validated records; evidence freshness bound: 120 seconds.
- Deduplication binds incident, category, and reason without exposing those values in the key.
- Critical categories include protected-invariant, writer-exclusivity, and post-boot failures.
- Unknown categories, malformed/tampered history, future records, and partial evidence fail closed.

## Safety analysis, rejected alternatives, rollback, and next dependency

The policy consumes supplied in-memory facts only. It cannot send a toast or external message, write a
journal, access a database, control WSL/systemd/the scheduler, or restart Windows. Deduplicating across
different incidents or suppressing severity escalation was rejected because it could hide distinct or
worsening failures.

Rollback is deletion of the implementation, focused test, and report. Phase 4HP should define bounded,
deterministic alert retry and backoff without performing delivery.
