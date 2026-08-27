# Phase 4HP — Alert Retry and Backoff Policy

## Outcome and measured evidence

Phase 4HP validates a Phase 4HO emit candidate and a hash-protected attempt history, then returns
`READY`, `WAIT`, `EXHAUSTED`, `DELIVERED`, `DENIED`, or `INCOMPLETE`. Retries use capped exponential
backoff with exact boundary semantics and a strict attempt budget. `READY` is only a retry candidate;
the policy never authorizes delivery or sends a notification.

Focused tests cover immediate first attempt, wait/exact eligibility, exponential cap, exhaustion,
delivered/incomplete/non-emittable terminal states, sequence/time/binding/future/post-delivery failures,
bounds and artifact tampering, and forbidden delivery/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hp-alert-retry-backoff-policy-v1`.
- Defaults: 30-second base delay, 300-second cap, and four total attempts.
- Delay after failed attempt N is `min(base × 2^(N-1), cap)`; exact eligibility time passes.
- Attempt numbers must begin at one and be consecutive; timestamps must strictly increase.
- Every attempt binds the exact Phase 4HO result hash; delivery is terminal and forbids later attempts.
- The decision binds ordered attempts, policy thresholds, timing, verdict, and denied capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

The policy consumes supplied in-memory artifacts only. It cannot deliver a toast or external message,
sleep, schedule work, write a journal, access a database, control WSL/systemd/the scheduler, or restart
Windows. Infinite retry and randomized unrecorded jitter were rejected because they impede bounded,
deterministic incident handling.

Rollback is deletion of the implementation, focused test, and report. Phase 4HQ should add aggregate
rate limiting and storm control across otherwise eligible alert candidates.
