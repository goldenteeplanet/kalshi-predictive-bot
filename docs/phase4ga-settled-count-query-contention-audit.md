# Phase 4GA — Settled-Count Query Contention Audit

## Outcome and measured evidence

Phase 4GA adds a deterministic offline audit of supplied settled-count query observations. It does
not execute the query. Each observation and the final result are hash protected, and the audit
requires one query fingerprint, source identity, and source watermark. Focused tests measure clear,
empty, exact-boundary, latency, busy-event, stale, malformed, partial, tampered, mixed-lineage, and
mutation-surface paths; Ruff and pytest evidence is reproducible from committed files.

## Contract and bounded work

- Schema: `phase4ga-settled-count-query-contention-audit-v1`.
- At most 64 observations are accepted by default; empty or oversized sets fail closed.
- Default inclusive limits are 300 seconds of observation age and 250 milliseconds of query
  duration. Exact limits are clear; one unit over is stale or contended.
- Any busy event or duration breach yields `CONTENDED`. A freshness breach takes precedence and
  yields `STALE`, preventing old evidence from claiming current contention or clearance.
- Canonical SHA-256 binds every sample plus the classification, thresholds, provenance, and
  permanent `execution_authorized=false` boundary.

## Safety, alternatives, rollback, and next dependency

The analyzer has no database, filesystem, network, publication, service-control, or exchange API.
Running benchmark queries against production and automatically creating an index were rejected
because they could create contention or mutate the guarded database. No UI integration is warranted
until a separate bounded collector supplies observations; unavailable evidence must remain explicit.

Rollback is deletion of the module, focused test, and report; no runtime rollback is required.
Phase 4GB should use this audit as artifact-only input to propose contention-safe query behavior
without executing DDL or changing production query scheduling.
