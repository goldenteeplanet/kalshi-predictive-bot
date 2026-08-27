# Phase 4GU — Dashboard Evidence Lane Comparison

## Outcome and measured evidence

Phase 4GU compares independent evidence lanes without merging disagreements away. It emits consistent,
divergent, incomplete, or stale outcomes and preserves deterministic agreement groups. Focused tests
cover ordering, empty and missing lanes, exact lane/freshness bounds, divergent verdicts, incomplete
lanes, malformed evidence, duplicates, mixed subjects, tampering, and immutable safety boundaries.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gu-dashboard-evidence-lane-comparison-v1`.
- Lanes bind subject, verdict/value hash, source identity/watermark, lineage hash, completeness, and age.
- Expected lane IDs are explicit; missing lanes yield `INCOMPLETE`, while unknown lanes fail closed.
- At least two and at most eight lanes are allowed by default. Evidence exactly 300 seconds old remains
  eligible; one second older produces `STALE`.
- Equal verdict/value pairs are grouped as `CONSISTENT`; any fresh disagreement is `DIVERGENT` with the
  independent groups retained. Canonical SHA-256 protects lanes and output.

## Safety, rejected alternatives, rollback, and next dependency

Comparison performs no database or exchange query, reconciliation write, artifact publication, or
service control and always emits `read_only=true` and `execution_authorized=false`. Majority voting and
automatic lane repair were rejected because they conceal authoritative disagreements.

Rollback is deletion of this module, focused test, and report. Phase 4GV should audit the accessibility
of the dashboard contracts and tokens introduced by Phases 4GN–4GU.
