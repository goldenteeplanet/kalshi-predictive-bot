# Phase 4IR — Keepalive restoration dry-run planner

Phase 4IR produces a symbolic keepalive restoration plan bound to a valid Phase 4IP `KEEPALIVE_RESTORE` capability, hashed health evidence, and a hashed target. Inactive or failed state yields verify, one restoration attempt, and heartbeat verification; active state yields a no-op heartbeat check.

Unknown state, wrong capability, incomplete evidence, binding mismatch, malformed data, or tampering fails closed. The plan inherits the 60-second ceiling and exactly-one-attempt rule.

The planner is read-only and dry-run-only. It never controls a scheduled task or service and grants no recovery, service-control, host-restart, order, or execution authority.
