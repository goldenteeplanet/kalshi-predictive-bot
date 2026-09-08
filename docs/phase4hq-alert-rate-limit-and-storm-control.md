# Phase 4HQ — Alert Rate-Limit and Storm Control

## Outcome and measured evidence

Phase 4HQ validates a Phase 4HP-ready retry candidate and bounded emission history, then returns
`ALLOW`, `RATE_LIMITED`, `STORM_SUPPRESSED`, `DENIED`, or `INCOMPLETE`. A short-window limit caps all
alerts, a lower noncritical limit reserves bounded capacity for critical alerts, and a longer hard storm
limit suppresses every severity. `ALLOW` remains an admission candidate and never delivers an alert.

Focused tests cover empty-history admission, critical reserve, short-window and exact expiry behavior,
hard storm limit and exact expiry, nonready/incomplete histories, bounds/duplicates/future events,
event/retry/result tampering, and forbidden delivery/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hq-alert-rate-limit-storm-control-v1`.
- Defaults: five alerts per 60 seconds, three noncritical alerts per 60 seconds, and 20 alerts per
  300-second storm window, with at most 512 history records.
- Events exactly at a window age are excluded; only strictly younger events consume capacity.
- Critical alerts may consume reserved short-window capacity but cannot bypass the total or storm cap.
- History is deterministically ordered and must have unique IDs, complete evidence, and no future time.
- The decision binds the Phase 4HP hash, counts, thresholds, ordered history, verdict, and denied powers.

## Safety analysis, rejected alternatives, rollback, and next dependency

The policy consumes supplied artifacts only. It cannot deliver or schedule a notification, write a
journal, access a database, control WSL/systemd/the scheduler, or restart Windows. Unlimited critical
bypass was rejected because a critical-alert loop could itself make the workstation unusable.

Rollback is deletion of the implementation, focused test, and report. Phase 4HR should define a
hash-protected operator acknowledgement contract without granting recovery or restart authority.
