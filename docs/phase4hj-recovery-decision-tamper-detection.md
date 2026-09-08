# Phase 4HJ — Recovery Decision Tamper Detection

## Outcome and measured evidence

Phase 4HJ validates a hash-protected recovery-decision record against a validated Phase 4HI canonical
bundle. It detects bundle substitution, readiness-claim mismatch, record mutation, replay/expiry,
future timestamps, excessive TTL, incomplete records, and action mismatch. A verified record means only
that `AWAIT_RECOVERY_POLICY` is correctly bound; it never authorizes recovery, service control, host
restart, or execution.

Focused tests cover the deterministic valid path, bundle and readiness tampering, exact expiration and
TTL boundaries, expiry, incomplete/future/wrong-action decisions, malformed values, decision/bundle/
report tampering, and forbidden query/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hj-recovery-decision-tamper-detection-v1`.
- Allowed actions are `AWAIT_RECOVERY_POLICY` and `DENY_RECOVERY`; neither performs an action.
- Decision TTL is at most 300 seconds; evaluation at the exact expiry instant passes.
- The Phase 4HI bundle must validate before any decision is examined.
- The record binds its bundle hash, readiness claim, action, target, timestamps, completeness, and source.
- The report redacts the decision identifier to a digest and binds all findings and denied capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

The detector consumes supplied artifacts only. It cannot query or control WSL, systemd, the scheduler,
a database, notifications, or Windows restart. Treating a valid signature as control authorization was
rejected because integrity proves provenance, not permission or operational safety.

Rollback is deletion of the implementation, focused test, and report. Phase 4HK should consolidate
Phases 4HA–4HJ into the WSL reliability workstream gate before any recovery planner is considered.
