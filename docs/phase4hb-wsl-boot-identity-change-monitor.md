# Phase 4HB — WSL Boot-Identity Change Monitor

## Outcome and measured evidence

Phase 4HB adds a deterministic monitor over independently supplied, hash-protected WSL boot-identity
observations. It distinguishes stable history, an identity transition, stale evidence, and incomplete
evidence. A transition requires an alert but never authorizes component recovery, service control,
host restart, or trading execution. Raw boot identities are not copied into the result.

Focused tests cover deterministic ordering, stable and changed identities, empty input, resource and
exact freshness boundaries, staleness, malformed sequence/time/identity fields, partial evidence,
mixed lineage, observation/result tampering, redaction, and the immutable safety boundary.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hb-wsl-boot-identity-monitor-v1`.
- Input: at most 128 consecutive observations from one source identity.
- Provenance: every observation and the result bind their canonical contents with SHA-256.
- Freshness: evidence exactly 120 seconds old passes; older evidence returns `STALE`.
- Ordering: sequence numbers must be consecutive and observation times strictly increase.
- Output: `STABLE`, `CHANGED`, `STALE`, or `DEGRADED`, transition count, redacted identity hashes,
  lineage, bounds, alert requirement, and explicit denied-control capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

The monitor consumes supplied values only. It cannot query or control WSL, processes, systemd, the
authoritative scheduler, notifications, a database, or Windows restart. Automatic restart on any boot
identity change was rejected: a legitimate operator reboot also changes identity, and change evidence
alone cannot establish a failure or authorize recovery.

Rollback is deletion of the implementation, focused test, and report. Phase 4HC should add a bounded,
read-only WSL liveness evidence collector while keeping observation and control capabilities separate.
