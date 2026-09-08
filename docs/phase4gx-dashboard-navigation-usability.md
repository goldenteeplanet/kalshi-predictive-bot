# Phase 4GX — Dashboard Navigation Usability

## Outcome and measured evidence

Phase 4GX adds a deterministic, artifact-only navigation-usability audit. Route evidence binds visible
labels, interaction count, keyboard reachability, current-location signaling, destination availability,
completeness, freshness, lineage, and SHA-256 integrity. Focused tests exercise deterministic ordering,
empty input, resource and exact threshold boundaries, stale evidence, malformed input, partial failure,
duplicates, mixed lineage, input/output tampering, and the immutable safety boundary.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gx-dashboard-navigation-usability-v1`.
- Duplicate route IDs or mixed source identity/watermarks fail closed.
- At most 64 routes are evaluated, entirely in memory, by default.
- Exactly three interactions and evidence exactly 300 seconds old pass; four interactions fail and
  evidence one second older becomes `STALE`.
- Missing labels, keyboard/current-location failures, unavailable destinations, and incomplete evidence
  produce stable route-specific reasons.

## Safety analysis, rejected alternatives, rollback, and next dependency

This phase evaluates supplied evidence and cannot navigate a browser, issue queries, publish artifacts,
control services, or mutate production state. It always emits `execution_authorized=false`. Automated
navigation and template modification were rejected because usability certification does not require
runtime control and shared UI files contain unrelated work.

Rollback is deletion of this module, focused test, and report. Phase 4GY should review export safety
using independently captured, hash-protected evidence.
