# Phase 4HA — WSL Keepalive Reliability Audit

## Outcome and measured evidence

Phase 4HA adds a deterministic audit over supplied WSL keepalive observations. Each observation binds
sequence/time, WSL and keepalive presence, user-systemd reachability, authoritative-service state,
completeness, freshness, lineage, and SHA-256 integrity. Tests cover healthy ordering, empty input,
resource and exact threshold bounds, staleness, degraded/partial evidence, malformed sequences, missing
links, mixed lineage, tampering, and immutable recovery/service-control boundaries.

## Contract, provenance, freshness, and bounds

- Schema: `phase4ha-wsl-keepalive-reliability-audit-v1`.
- At most 128 consecutive observations are evaluated in memory by default.
- Observation sequences and timestamps must be strictly consecutive/monotonic and share one identity.
- A keepalive gap of exactly 60 seconds and evidence exactly 120 seconds old pass; exceeding either
  produces `DEGRADED` or `STALE` respectively.
- Missing WSL, keepalive, user-systemd, authoritative-service, or completeness evidence fails closed.

## Safety analysis, rejected alternatives, rollback, and next dependency

The audit consumes evidence only. It cannot invoke WSL, inspect processes, control systemd, publish an
artifact, or access a database, and explicitly denies recovery, service control, and execution. An
automatic keepalive restarter was rejected because runtime control belongs exclusively to the operator
and authoritative service arrangement.

Rollback is deletion of this module, focused test, and report. Phase 4HB should monitor WSL boot
identity changes from independently collected, hash-protected evidence.
