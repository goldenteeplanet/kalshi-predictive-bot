# Phase 4GT — Dashboard Settlement Timeline

## Outcome and measured evidence

Phase 4GT adds a deterministic, artifact-only settlement lifecycle timeline. It calculates bounded dwell
durations while distinguishing pending settlement, observed settlement, incomplete lineage, and stale
evidence. Focused tests cover deterministic order, empty input, event bounds, exact freshness, settled
and pending paths, missing stages, incomplete events, malformed timestamps, duplicates, stage order,
mixed lineage, tampering, and immutable safety boundaries.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gt-dashboard-settlement-timeline-v1`.
- Events bind lifecycle stage, timestamp, order/ticker/forecast identity, source identity/watermark,
  upstream lineage hash, completeness, age, and SHA-256.
- Stages progress monotonically from filled through awaiting, observed, reconciled, and realized. Missing
  intermediate stages yield `INCOMPLETE`; backward or duplicate stages fail closed.
- The default maximum is 32 events. Evidence exactly 300 seconds old remains eligible; one second older
  produces `STALE`. Dwell times are computed only from supplied timestamps.

## Safety, rejected alternatives, rollback, and next dependency

The timeline performs no settlement sync, database query, artifact write, service control, or execution
action and always emits `read_only=true` and `execution_authorized=false`. Polling the exchange and
repairing missing lifecycle rows were rejected because they broaden authority.

Rollback is deletion of this module, focused test, and report. Phase 4GU should compare independent
evidence lanes against the same order and lineage identity without merging disagreements away.
