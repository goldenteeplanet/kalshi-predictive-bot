# Phase 4GL — Evidence Query Plan Drift Detector

## Outcome and measured evidence

Phase 4GL compares supplied baseline/current plan hashes linked to a ready Phase 4GK corpus. Focused
tests cover stable order-independent input, empty and partial cases, bounds, exact freshness, drift,
stale suppression, malformed, duplicate, tampered, lineage-mismatched, stale-corpus, and safety
boundary paths. Ruff and pytest evidence is reproducible from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gl-evidence-query-plan-drift-detector-v1`.
- At most 256 unique query observations are accepted, each linked to the exact corpus hash and source
  identity/watermark.
- Matching plan hashes yield `STABLE`; any fresh mismatch yields `DRIFT` with sorted fingerprints.
- Evidence exactly 300 seconds old is eligible. One second older yields `STALE` and suppresses drift
  detail so old evidence cannot claim current regressions.
- Empty, malformed, partial, duplicate, tampered, unlinked, or stale-corpus input fails closed.
  Canonical SHA-256 binds observations, corpus, lineage, bounds, and `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

The detector cannot run `EXPLAIN`, open SQLite, execute SQL, change an index, publish artifacts,
control services, or contact an exchange. Automatic plan capture and automatic index creation were
rejected because they introduce production query load or mutation.

Rollback is deletion of the module, focused test, and report. Phase 4GM should use this detector as
artifact-only evidence for a query-plan review gate without applying optimizer changes.
