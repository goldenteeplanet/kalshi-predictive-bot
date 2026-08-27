# Phase 4HK — WSL Reliability Workstream Gate

## Outcome and measured evidence

Phase 4HK certifies the first resilient-operations workstream only when the Phase 4HA keepalive audit is
healthy, the exact Phase 4HB–4HH canonical bundle is ready, and the Phase 4HJ decision report is
verified, bundle-bound, fresh, and unexpired. Certification attests to supplied evidence quality and
does not authorize recovery, service control, host restart, or execution.

Focused tests cover deterministic certification, unhealthy and incomplete keepalive evidence,
unverified decisions, exact freshness/expiry boundaries, staleness, expiry, invalid bounds, upstream
tampering, bundle-binding mismatch, result tampering, and forbidden query/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hk-wsl-reliability-workstream-gate-v1`.
- Required chain: validated 4HA audit, validated 4HI bundle, validated 4HJ report.
- Evidence exactly 120 seconds old and evaluation at exact decision expiry pass.
- All three upstream hashes are bound in a fixed-order evidence-chain digest.
- Any unhealthy, non-ready, unverified, expired, incomplete, stale, or mismatched input fails closed.
- The result binds thresholds, evaluation time, lineage hashes, verdict, and denied capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

The gate consumes supplied artifacts only. It cannot query WSL, processes, systemd, the scheduler, or
a database; send notifications; or control/restart anything. Enabling recovery at this workstream gate
was rejected because alerting, classification, cooldown, cancellation, and post-boot phases remain.

Rollback is deletion of the implementation, focused test, and report. Phase 4HL should define the
versioned local incident-journal schema used by the alerting workstream.
