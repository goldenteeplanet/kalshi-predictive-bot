# Phase 4HU — Alerting workstream gate

Phase 4HU closes the alerting workstream with a deterministic fail-closed evidence gate. Readiness requires exactly one complete, integrity-verified artifact for severity/deduplication, retry/backoff, rate-limit/storm control, operator acknowledgement, recovery cancellation, and delivery audit export.

Missing or incomplete evidence yields `INCOMPLETE`, unverified evidence yields `NOT_READY`, and duplicate component evidence yields `TAMPERED`. Canonical component ordering and hashes make the decision reproducible and tamper-evident.

`READY` certifies only that the alerting evidence workstream is complete. It does not authorize sending an alert, recovery, service control, WSL or Windows restart, order creation, or execution. The gate is pure and read-only.
