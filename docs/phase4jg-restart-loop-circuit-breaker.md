# Phase 4JG — Restart-loop circuit breaker

Phase 4JG prevents repeated automatic host restarts for the same incident. The breaker is closed only for a complete, integrity-verified incident state with no prior restart attempt, no post-boot verification outstanding, no automatic-recovery disablement, and no operator-intervention requirement.

One consumed incident attempt, pending or failed post-boot verification, disabled automation, or an operator requirement opens the breaker. Incomplete or unverified state is denied; contradictory, malformed, or tampered state fails closed. A failed post-boot state is valid only when automation is disabled and operator intervention is required.

`CLOSED` does not authorize a restart. The evaluator is read-only and performs no persistence, notification, process, service-control, or restart operation.
