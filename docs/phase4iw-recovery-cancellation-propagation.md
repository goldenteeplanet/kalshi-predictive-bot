# Phase 4IW — Recovery cancellation propagation

Phase 4IW propagates a validated Phase 4HS cancellation through a bounded set of child-plan states. Pending or running cancelable children transition symbolically to `CANCELLED`; completed, failed, or already-cancelled children remain terminal. If no active child exists, the result is an idempotent no-op.

Any active non-cancelable child denies the entire propagation, preventing partial cancellation. Incomplete states, duplicate child identities, parent mismatches, excessive children, invalid upstream cancellation, malformed data, or tampering fails closed.

The propagator is read-only and never executes a cancellation. It grants no recovery, service-control, host-restart, order, or execution authority.
