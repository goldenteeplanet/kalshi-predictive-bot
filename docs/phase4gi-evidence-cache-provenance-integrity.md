# Phase 4GI — Evidence Cache Provenance Integrity

## Outcome and measured evidence

Phase 4GI verifies an ordered, hash-linked provenance record for every cache entry measured by Phase
4GH. It cross-checks the exact descriptor manifest, count, source identity, watermark, chain links,
content hashes, and freshness. Focused tests cover intact, empty, partial, exact-boundary, stale,
malformed, tampered, broken-chain, manifest/count mismatch, and safety-boundary paths. Ruff and pytest
evidence is reproducible from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gi-evidence-cache-provenance-integrity-v1`.
- At most 256 provenance records are accepted, exactly matching the 4GH entry count and manifest.
- Each record binds its full descriptor, content hash, and predecessor hash. Genesis has no
  predecessor; every later record must point to the preceding record hash.
- Evidence exactly 300 seconds old is eligible; one second older is `STALE`.
- Missing, malformed, tampered, reordered, unlinked, or partial evidence fails closed. Canonical
  SHA-256 binds the chain, bounds artifact, source lineage, freshness, and
  `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

The verifier consumes supplied artifacts only and cannot read payloads, access or mutate a cache,
run SQL, publish artifacts, control services, or contact an exchange. Trusting keys without content
hashes and repairing a broken chain automatically were rejected because they conceal integrity loss.

Rollback is deletion of the module, focused test, and report. Phase 4GJ should compose 4GH memory
bounds and 4GI provenance integrity into a deterministic cache-readiness gate.
