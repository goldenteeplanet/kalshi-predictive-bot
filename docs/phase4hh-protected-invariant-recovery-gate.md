# Phase 4HH — Protected-Invariant Recovery Gate

## Outcome and measured evidence

Phase 4HH validates the complete protected production baseline and a validated Phase 4HG writer gate.
The baseline binds paper-order count, position-sizing and advanced-risk maxima/counts, order 204 status,
ticker and quantity, forecast 523912, one fill, and Phase 3M/3N IDs 231/231. Passing proves both
prerequisites for supplied evidence but still does not authorize recovery, service control, restart,
or execution.

Focused tests cover the exact baseline, individual invariant regressions, exact freshness, staleness,
incomplete evidence, failed writer prerequisite, malformed values, observation/writer/result tampering,
and forbidden database/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hh-protected-invariant-recovery-gate-v1`.
- The full baseline is exact; aggregate similarity cannot mask an individual regression.
- Writer evidence must pass the Phase 4HG validator and prerequisite.
- Evidence exactly 120 seconds old passes; older evidence returns `STALE`.
- The result binds a canonical invariant snapshot digest, source hash, observation hash, writer-gate hash,
  freshness threshold, verdict, and denied capabilities.

## Safety analysis, rejected alternatives, rollback, and next dependency

This gate consumes supplied values only. It cannot access or mutate a database, query or control the
scheduler, send notifications, or restart Windows. Treating a matching count aggregate as sufficient
was rejected because order lineage and every protected identifier must remain exact.

Rollback is deletion of the implementation, focused test, and report. Phase 4HI should canonicalize
the complete recovery evidence chain while retaining provenance, freshness, and denial semantics.
