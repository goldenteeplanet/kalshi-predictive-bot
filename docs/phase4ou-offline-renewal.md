# Phase 4OU — Offline Evidence Renewal Orchestration

## Outcome

Phase 4OU refreshes only expired offline-eligible evidence in the dependency order defined by the
handoff catalog. Each deterministic renewal binds the predecessor record, renewed dependency proof
hashes, and renewal timestamp. The authoritative settlement record never enters the offline
pipeline and is preserved byte-for-byte.

The verifier replays the entire orchestration from the original records and timestamps. Partial,
stale, circular, reordered, replayed, altered, clock-regressed, or nondeterministic renewal results
fail closed. The rebuilt handoff remains settlement-blocked and non-executable.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OT/4OU regression: `12 passed in 32.63s`.
- Offline renewals: `6`, in declared dependency order.
- Settlement record unchanged: `true`; post-handoff settlement ready: `false`; executable: `false`.
- Orchestration SHA-256:
  `199cd825cb673fcbd1ea5b2136627a08fb998b10934c30a28491ca178ae79ed7`.
- Replay verification: `PASS`; SHA-256:
  `cf6fb522d95c966bf71af0e24fc66e7bddcdc7f7c37b76b4d186883057ae48e0`.

## Safety and removal

Renewal is offline, in-memory, and non-persistent. It cannot create paper orders or enable demo,
live, or autopilot execution. Remove the three Phase 4OU files to roll back.

## Next phase

Phase 4OV — Renewal interruption, checkpoint, and exactly-once resume proof.
