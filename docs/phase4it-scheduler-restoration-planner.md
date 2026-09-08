# Phase 4IT — Scheduler restoration planner

Phase 4IT produces a symbolic scheduler restoration plan bound to a valid Phase 4IP `SCHEDULER_RESTORE` capability, scheduler evidence, writer-exclusivity evidence, and a hashed target. Inactive or failed state yields exclusivity verification, one user-scheduler restoration attempt, heartbeat verification, and protected-invariant verification. Active state yields verification only.

Unknown state or unproven writer exclusivity is denied. Wrong capability, incomplete evidence, binding mismatch, malformed data, or tampering fails closed. The plan inherits the 120-second ceiling and exactly-one-attempt rule.

The planner is read-only and dry-run-only. It never controls a scheduler, task, or service and grants no recovery, host-restart, order, or execution authority.
