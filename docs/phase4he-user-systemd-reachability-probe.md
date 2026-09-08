# Phase 4HE — User-Systemd Reachability Probe

## Outcome and measured evidence

Phase 4HE canonicalizes independently supplied user-systemd probe observations into deterministic,
hash-protected reachability evidence. It binds manager reachability, runtime-directory and D-Bus-session
presence, exit status, duration, freshness, probe/source lineage, and a digest of bounded output.
Evidence is `REACHABLE`, `UNREACHABLE`, `STALE`, or `INCOMPLETE`; every non-reachable state requires
an alert but never authorizes recovery, service control, host restart, or execution.

Focused tests cover reachable and simultaneous-unreachable paths, exact duration/freshness boundaries,
staleness, incomplete evidence, absent exit status, malformed and oversized input, deterministic output,
tampering, redaction, and forbidden subprocess/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4he-user-systemd-reachability-probe-v1`.
- Probe output is bounded to 4,096 characters and retained only as SHA-256.
- Evidence exactly 120 seconds old and a probe exactly 5,000 milliseconds long pass.
- Missing manager, runtime-directory, or D-Bus evidence in a complete probe is `UNREACHABLE`.
- Incomplete evidence takes precedence and does not infer a root cause.
- Observation and evidence hashes bind thresholds, canonical content, provenance, and denied controls.

## Safety analysis, rejected alternatives, rollback, and next dependency

This phase accepts supplied observations only. It cannot invoke `systemctl`, WSL, subprocesses, the
scheduler, notifications, a database, or Windows restart. Automatically starting a user manager was
rejected because reachability evidence alone does not authorize control and could create a second
writer path.

Rollback is deletion of the implementation, focused test, and report. Phase 4HF should add a bounded,
read-only authoritative-scheduler health probe with explicit unit identity and writer-safety evidence.
