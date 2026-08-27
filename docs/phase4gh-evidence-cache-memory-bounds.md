# Phase 4GH — Evidence Cache Memory Bounds

## Outcome and measured evidence

Phase 4GH evaluates supplied, hash-protected cache-entry descriptors against explicit memory and
freshness limits linked to a ready Phase 4GG proposal. Focused tests cover valid, empty, partial,
exact-boundary, count/total/per-entry breaches, deterministic eviction advice, stale evidence,
malformed, duplicate, tampered, lineage-mismatched, and safety-boundary cases. Ruff and pytest
results are reproducible from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gh-evidence-cache-memory-bounds-v1`.
- Input processing is hard-capped at 256 entries. Default cache limits are 64 entries, 1 MiB total,
  and 64 KiB per entry.
- Evidence exactly 300 seconds old is eligible; one second older yields `STALE` and deliberately
  suppresses eviction advice.
- Fresh breaches yield `EVICTION_REQUIRED` with deterministic, advisory keys ordered by oversize,
  age, size, and key until all limits would be met. Exact limits yield `WITHIN_BOUNDS`.
- Canonical SHA-256 binds every descriptor, proposal and source lineage, limits, classification,
  eviction advice, and `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

The evaluator cannot inspect process memory, read or mutate a cache, evict an entry, execute SQL,
publish artifacts, control services, or contact an exchange. Automatic eviction and unbounded object
introspection were rejected because they are stateful or resource-unsafe. UI integration is omitted
until an opt-in observation collector exists.

Rollback is deletion of the module, focused test, and report. Phase 4GI should validate an explicit
cache-retention proposal against these bounds without performing eviction.
