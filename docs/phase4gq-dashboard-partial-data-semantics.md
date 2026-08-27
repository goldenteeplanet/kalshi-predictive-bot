# Phase 4GQ — Dashboard Partial-Data Semantics

## Outcome and measured evidence

Phase 4GQ distinguishes complete, valid-empty, partial, pending, deferred, unavailable, and failed panel
data over the Phase 4GP loading contract. Focused tests cover deterministic order, empty and partial
input, component boundaries, partial component failure, staleness, truncation, malformed counts,
result-count disagreement, lineage mismatch, tampering, and immutable safety boundaries.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gq-dashboard-partial-data-semantics-v1`.
- Evidence binds panel ID, observed items, successful/total components, truncation, lineage, and age.
- The evidence set must exactly match the loading contract; the default limits are 32 panels and 16
  components per panel.
- A loaded zero-item result with all components successful is `EMPTY`, not unavailable. Failed
  components or truncation are `PARTIAL`; loading errors and timeouts are `FAILED`.
- Evidence exactly 300 seconds old remains eligible; one second older makes all panels unavailable and
  the aggregate `STALE`. Canonical SHA-256 binds each input and final result.

## Safety, rejected alternatives, rollback, and next dependency

The evaluator is pure, read-only computation and always emits `execution_authorized=false`. Treating
zero rows as failure and merging partial data into a complete result were rejected because both conceal
important operator evidence.

Rollback is deletion of this module, focused test, and report. Phase 4GR should map freshness and these
partial-data states into deterministic, accessible visualization tokens.
