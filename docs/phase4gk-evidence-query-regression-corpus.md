# Phase 4GK — Evidence Query Regression Corpus

## Outcome and measured evidence

Phase 4GK defines a deterministic, hash-protected corpus of query fingerprints, fixture hashes,
expected-result hashes, source lineage, and evidence ages. It validates the corpus without executing
queries. Focused tests cover ready, empty, partial, exact-boundary, stale, malformed, duplicate,
tampered, mixed-lineage, distinct-query-count, and safety-boundary paths. Ruff and pytest evidence is
reproducible from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gk-evidence-query-regression-corpus-v1`.
- At most 256 cases are accepted by default. Case IDs are unique and canonical ordering makes the
  manifest independent of input order.
- Every case binds a query fingerprint, fixture and expected-result hashes, and one homogeneous
  source identity/watermark.
- Evidence exactly 86,400 seconds old is eligible; one second older makes the corpus `STALE`.
- Empty, malformed, partial, duplicate, mixed, or tampered cases fail closed. Canonical SHA-256 binds
  all cases, bounds, lineage, status, and `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

The corpus builder cannot execute SQL, open a database, load fixtures, publish artifacts, control
services, or contact an exchange. Capturing production query output automatically and storing raw
payloads were rejected because they introduce runtime access and potentially sensitive bulk data.

Rollback is deletion of the module, focused test, and report. Phase 4GL should compare supplied
replay-result hashes against this corpus without running the query itself.
