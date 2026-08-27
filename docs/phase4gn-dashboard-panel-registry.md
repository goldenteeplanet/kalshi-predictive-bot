# Phase 4GN — Dashboard Panel Registry

## Outcome and measured evidence

Phase 4GN adds a deterministic registry for read-only dashboard panels. The registry orders panels by
explicit priority, binds their routes and upstream artifact lineage, and reports `READY`, `BLOCKED`, or
`STALE` without loading panel data. Focused tests cover stable ordering, empty input, panel and freshness
boundaries, required and optional disablement, malformed routes, duplicate identity, mixed lineage,
tampering, and immutable read-only/execution boundaries.

## Inputs, outputs, provenance, freshness, and bounds

- Schema: `phase4gn-dashboard-panel-registry-v1`.
- Each descriptor binds panel identity, local route, title, upstream schema, source identity/watermark,
  evidence age, unique priority, required state, and enabled state with canonical SHA-256.
- The default registry accepts at most 32 panels. Duplicate IDs, routes, or priorities fail closed.
- All descriptors must share one source identity and watermark. Evidence at 300 seconds remains
  eligible; one second older produces `STALE`.
- A disabled required panel produces `BLOCKED`; a disabled optional panel remains visible in counts but
  does not claim availability. The output always has `read_only=true` and
  `execution_authorized=false`.

## Safety, rejected alternatives, rollback, and next dependency

Registry construction performs no HTTP request, database query, artifact publication, service control,
or execution action. Eagerly loading every panel and editing the currently dirty shared navigation were
rejected: both would add latency or risk overwriting unrelated UI work. The registry is directly
importable by the existing UI when the navigation owner integrates it.

Rollback is deletion of the registry module, focused test, and report. Phase 4GO should consume this
registry to define progressive disclosure without triggering hidden expensive work.
