# Phase 4GM — Evidence Latency Workstream Gate

## Outcome and measured evidence

Phase 4GM adds an artifact-only gate for the Phase 4GH–4GL evidence-latency chain. It normalizes the
five upstream results into hash-protected envelopes and emits `READY`, `BLOCKED`, or `STALE` without
opening a database, running a query, publishing an artifact, or granting execution authority.
Focused tests cover deterministic ordering, empty and partial input, exact count and freshness bounds,
staleness, non-ready and incomplete evidence, malformed input, duplicates, mixed lineage, tampering,
and the immutable safety boundary.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gm-evidence-latency-workstream-gate-v1`.
- Exactly one envelope is required for memory bounds, provenance integrity, cold-start seeding,
  regression corpus, and plan drift; no more than five envelopes are accepted by default.
- Every envelope binds its upstream schema, artifact hash, source identity, source watermark, outcome,
  completeness, and age with canonical SHA-256.
- All source identities and watermarks must match. Evidence exactly 300 seconds old is eligible; one
  second older makes the entire gate `STALE` and prevents a readiness claim.
- `READY` requires `WITHIN_BOUNDS`, `INTACT`, `PROPOSE`, `READY`, and `STABLE`, respectively. Missing,
  malformed, duplicated, tampered, incomplete, mixed-lineage, or non-ready evidence fails closed.

## Safety, rejected alternatives, rollback, and next dependency

The implementation is pure computation over caller-supplied immutable values and always emits
`execution_authorized=false`. Direct database inspection, automatic cache warming, optimizer changes,
and automatic artifact publication were rejected because they would broaden authority or add load.
Callers must validate each upstream artifact before constructing its envelope.

Rollback is deletion of this module, focused test, and report. Phase 4GN can register this gate as an
opt-in dashboard panel while preserving unavailable, blocked, and stale states honestly.
