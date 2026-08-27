# Phase 4HF — Authoritative Scheduler Health Probe

## Outcome and measured evidence

Phase 4HF canonicalizes independently supplied scheduler observations into deterministic,
hash-protected health evidence. A healthy verdict requires the exact
`kalshi-fixed-rate-refresh.service` identity, loaded/active/running state, positive main PID, complete
fresh evidence, bounded probe duration, and exactly one observed writer. Every non-healthy result
requires an alert and explicitly denies recovery, service control, host restart, and execution.

Focused tests cover healthy, simultaneous unhealthy, writer-violation, identity-mismatch, stale,
incomplete, missing-writer, exact duration/freshness, malformed and oversized input, deterministic
output, tampering, and forbidden service/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hf-authoritative-scheduler-health-probe-v1`.
- Authoritative identity: `kalshi-fixed-rate-refresh.service` exactly.
- Probe output is bounded to 4,096 characters and retained only as SHA-256.
- Evidence exactly 120 seconds old and duration exactly 5,000 milliseconds pass.
- Missing or non-exclusive writer evidence prevents a healthy verdict.
- Observation and evidence hashes bind canonical content, thresholds, provenance, and denied controls.

## Safety analysis, rejected alternatives, rollback, and next dependency

This phase accepts supplied observations only. It cannot invoke systemd, inspect processes, control the
scheduler, send notifications, access a database, or restart Windows. Automatically restarting an
unhealthy scheduler was rejected because writer-exclusivity and invariant gates must independently
authorize any future recovery action.

Rollback is deletion of the implementation, focused test, and report. Phase 4HG should combine
independent writer evidence into a fail-closed writer-exclusivity recovery gate.
