# Phase 4GG — Evidence Cache Single-Flight Proposal

## Outcome and measured evidence

Phase 4GG converts the Phase 4GF advisory policy into a complete, deterministic participant-role
proposal linked to the Phase 4GE deadline. Focused tests cover leader, follower, and cache-reader
roles; empty and partial inputs; exact age/wait limits; stale and overlong waits; malformed,
duplicate, count-mismatched, tampered, and cross-linked inputs; and absence of cache/query/mutation
calls. Ruff and pytest evidence is reproducible from committed files.

## Contract, freshness, provenance, and bounds

- Schema: `phase4gg-evidence-cache-single-flight-proposal-v1`.
- At most 32 unique participants are accepted, with exact cardinality matching the 4GF policy.
- A cache hit assigns every participant `CACHE_READER`; a live lease assigns `FOLLOWER`; an election
  assigns exactly one `LEADER` and all others `FOLLOWER`.
- Policy evidence is fresh through 60 seconds and follower wait may equal, but never exceed, the
  propagated remaining deadline. One-unit breaches reject the proposal.
- Canonical SHA-256 binds policy and propagation hashes, source lineage, all roles, limits, decision,
  reasons, and `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

This is a proposal only. It cannot create a future, acquire a lock or lease, read or write a cache,
run a query, publish an artifact, control services, or contact an exchange. An executable in-process
single-flight implementation and implicit waiting were rejected because they add state and runtime
effects before outcome validation.

Rollback is deletion of the module, focused test, and report. Phase 4GH should validate supplied
single-flight outcome observations against this proposal before any implementation is considered.
