# Phase 4HG — Writer-Exclusivity Recovery Gate

## Outcome and measured evidence

Phase 4HG combines validated Phase 4HF scheduler evidence with an independent, hash-protected writer
inventory. The prerequisite passes only when both sources identify exactly one writer and that writer
is exactly `kalshi-fixed-rate-refresh.service`. Passing proves writer exclusivity for the supplied
evidence but intentionally does not authorize recovery, service control, host restart, or execution.

Focused tests cover the valid prerequisite, zero/multiple/unexpected writers, exact freshness,
staleness, incomplete inventory, authoritative-identity mismatch, duplicate/malformed identities,
tampered inventory, tampered scheduler evidence, result tampering, and forbidden control surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hg-writer-exclusivity-recovery-gate-v1`.
- Scheduler evidence must pass the Phase 4HF validator before use.
- The inventory and scheduler evidence must both prove one exact authoritative writer.
- Evidence exactly 120 seconds old passes; older evidence returns `STALE`.
- Writer identities are represented by an ordered canonical digest in the result.
- Inventory and result hashes bind canonical content, source hashes, evidence hashes, and denied controls.

## Safety analysis, rejected alternatives, rollback, and next dependency

This is a prerequisite gate, not a control authorization. It consumes supplied values only and cannot
query processes, control systemd or the scheduler, access a database, send notifications, or restart
Windows. Allowing recovery from writer evidence alone was rejected because protected production
invariants must independently pass before any future action can be considered.

Rollback is deletion of the implementation, focused test, and report. Phase 4HH should add the
independent protected-invariant recovery gate and preserve the separation from actual service control.
