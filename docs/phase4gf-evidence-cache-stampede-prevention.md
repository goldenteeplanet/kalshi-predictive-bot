# Phase 4GF — Evidence Cache Stampede Prevention

## Outcome and measured evidence

Phase 4GF adds a deterministic advisory cache-refresh election based on a Phase 4GE deadline and a
hash-protected cache snapshot. Focused tests cover fresh hits, empty and partial inputs, bounds,
exact cache and lease thresholds, stale election, deterministic ordering, malformed data, tampering,
lineage mismatch, and absence of cache/query/mutation calls. Ruff and pytest results are reproducible
from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gf-evidence-cache-stampede-prevention-v1`.
- At most 32 unique requesters are accepted. Empty, duplicate, oversized, malformed, tampered, or
  unlinked inputs fail closed.
- Cache age is fresh through 60 seconds by default. A stale cache with an in-flight lease through
  30 seconds returns `WAIT`; a lease one second older is expired.
- With stale cache and no live lease, exactly one requester is elected by canonical SHA-256 ordering,
  independent of input order. This is advisory and grants no refresh or execution authority.
- Canonical SHA-256 binds deadline, cache snapshot, source lineage, thresholds, election result, and
  `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

The policy cannot acquire or release a lease, read or write a cache, open SQLite, dispatch a refresh,
publish artifacts, control services, or contact an exchange. Random election, automatic lease
acquisition, and default-path UI refresh were rejected because they are nondeterministic or stateful.

Rollback is deletion of the module, focused test, and report with no runtime action. Phase 4GG should
validate cache-coalescing outcomes against this policy using supplied observations only.
