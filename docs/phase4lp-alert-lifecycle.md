# Phase 4LP — Alert Lifecycle State Machine

## Outcome

Phase 4LP reconstructs an alert lifecycle from an immutable Phase 4LO envelope and ordered,
hash-chained transition evidence. Exact event replay is idempotent; conflicting replay fails closed.

## State invariants

Valid progress begins at `CREATED`, passes through `VALIDATED`, and may reach `PRESENTED` before a
terminal state. `ACKNOWLEDGED`, `EXPIRED`, `REJECTED`, and `ARCHIVED` are immutable. Skipped,
reversed, envelope-mismatched, malformed, conflicting, or post-terminal transitions cause refusal.

## Safety and removal

The state machine reconstructs and validates supplied evidence only. It cannot deliver alerts,
persist state, access a network, control services or WSL, or create orders. Remove the script,
focused test, and report to roll back.

## Next phase

Phase 4LQ — Alert lifecycle retention and privacy-minimization contract.
