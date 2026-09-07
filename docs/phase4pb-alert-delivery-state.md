# Phase 4PB — Alert Delivery State-Machine Proof

## Outcome

Phase 4PB models alert delivery entirely offline. It binds acknowledgements and timeouts to a
specific attempt, applies deterministic exponential retry timing, escalates after three failed
attempts, and reconstructs identical state from an immutable event sequence after a process
restart.

Identical duplicate events are idempotent. Conflicting duplicates, delayed acknowledgements from
an older attempt, reordered events, early retries, malformed or corrupted events, and concurrent
terminal transitions fail closed. The model records dry-run intent only: it sends no notification,
restarts no service, changes no runtime state, and cannot create an order.

## Safety and removal

All state is in-memory and deterministic. Paper order creation, demo execution, live execution,
and autopilot remain disabled. Remove the three Phase 4PB files to roll back.

## Verification evidence

- Combined Phase 4PA/4PB regression: `11 passed in 109.94s`
- Post-hardening Phase 4PB regression: `6 passed in 22.95s`
- Lost acknowledgement retried: `true`
- Restart replay identical: `true`
- Retry exhaustion escalated: `true`
- Real notifications sent: `0`
- Successful-state SHA-256: `8b9c3273f9ddb3b6feeb19fcf138ae3fe5ceac6e283bf38ef9a2cf4079fab32f`
- Exhausted-state SHA-256: `b20bd6d5f9a84ca7c336e463f769eb30e9139d65ff5db9e8dc9cfe9ff024e5ba`
- Certificate SHA-256: `11e5bce8701225bfd4eceb91f4c72ddb2266159a573cb35f340220d6dbec8c2b`
- Verdict: `PASS`

## Next phase

Phase 4PC — Alert outbox durability, atomic checkpoint, and crash-window proof.
