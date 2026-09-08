# Phase 4HC — WSL Liveness Evidence Collector

## Outcome and measured evidence

Phase 4HC canonicalizes a bounded, independently supplied WSL liveness probe result into deterministic,
hash-protected evidence. It records availability, distribution state, duration, exit status, freshness,
probe/source lineage, and a digest of bounded probe output without retaining raw output. Results are
`AVAILABLE`, `UNAVAILABLE`, `STALE`, or `INCOMPLETE`; every non-available result requires an alert but
does not authorize recovery, service control, host restart, or execution.

Focused tests cover the available path, simultaneous failures, exact duration/freshness boundaries,
staleness, incomplete evidence, missing exit status, malformed and oversized input, tampering,
determinism, output redaction, and forbidden control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hc-wsl-liveness-evidence-v1`.
- Probe output is bounded to 4,096 characters and represented only by SHA-256.
- Evidence exactly 120 seconds old and a probe lasting exactly 5,000 milliseconds pass.
- Older evidence returns `STALE`; a longer complete probe returns `UNAVAILABLE` with a timeout reason.
- Incomplete probe evidence takes precedence and returns `INCOMPLETE` without inferring root cause.
- Probe/result hashes bind canonical fields, thresholds, provenance, and denied capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

This collector accepts a supplied probe result and performs no subprocess, WSL, process, systemd,
scheduler, notification, database, or restart operation. Directly launching `wsl.exe` here was rejected
to preserve the separation between evidence collection and host control and to keep tests air-gapped.

Rollback is deletion of the implementation, focused test, and report. Phase 4HD should classify
keepalive gaps from bounded, hash-protected evidence without performing recovery.
