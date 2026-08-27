# Phase 4HN — Windows Toast Capability Probe

## Outcome and measured evidence

Phase 4HN canonicalizes independently supplied Windows toast capability facts into deterministic,
hash-protected evidence. Availability requires Windows, an interactive session, enabled notifications,
a registered application identity, an available toast API, complete fresh evidence, and bounded probe
duration. The probe never displays a notification or authorizes alert delivery.

Focused tests cover available and simultaneous-unavailable paths, exact duration/freshness boundaries,
staleness, incomplete evidence, malformed and oversized input, deterministic output, observation/result/
safety tampering, redaction, and forbidden toast/subprocess/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hn-windows-toast-capability-probe-v1`.
- Probe output is bounded to 4,096 characters and retained only as SHA-256.
- Evidence exactly 300 seconds old and a probe exactly 5,000 milliseconds long pass.
- Missing platform, session, notification, application identity, or API capability is `UNAVAILABLE`.
- Incomplete evidence takes precedence and does not infer capability.
- Observation and evidence hashes bind thresholds, canonical facts, provenance, and denied capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

This phase accepts supplied observations only. It cannot instantiate a toast API, send a notification,
run a subprocess, access a database, control WSL/systemd/the scheduler, or restart Windows. Sending a
test toast was rejected because capability assessment does not authorize user-visible side effects.

Rollback is deletion of the implementation, focused test, and report. Phase 4HO should define
deterministic alert severity and deduplication before any delivery mechanism is considered.
