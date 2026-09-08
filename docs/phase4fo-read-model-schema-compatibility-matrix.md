# Phase 4FO — Read-Model Schema Compatibility Matrix

## Outcome

Phase 4FO adds a deterministic, hash-protected compatibility matrix for read-model producer and
consumer schema identities. Compatibility is never inferred from version text: an exact pair must
be declared, and declared incompatibility remains a first-class result.

## Contract

- Input: an in-memory matrix artifact, producer schema, consumer schema, trusted current time,
  maximum age, and maximum entry count.
- Output: a frozen result containing the declared decision, reason, matrix hash, and age.
- Identity: `phase4fo-read-model-compatibility-matrix-v1`.
- Provenance: a mandatory SHA-256 provenance hash.
- Freshness: matrix age must be strictly below the configured threshold; equality is stale.
- Bounds: entry count is caller-bounded and validated before pair lookup.
- Determinism: entries are unique and lexicographically ordered by producer/consumer identity.
- Integrity: a canonical SHA-256 covers every matrix field except the hash itself.
- Failure: missing, malformed, duplicate, out-of-order, stale, future-dated, tampered, oversized,
  or undeclared evidence raises a stable `CompatibilityMatrixError`.

## Safety analysis

The module has no database, network, exchange, service-control, artifact-publication, or mutation
imports. It operates only on caller-supplied mappings and trusted time. Unknown schema pairs fail
closed; no fallback database query is available.

## Verification evidence

Deterministic tests cover compatible and explicitly incompatible paths, empty matrices, the exact
freshness boundary, undeclared pairs, tampering, malformed/partial input, duplicate entries,
chronological ordering, entry bounds, and a mutation-surface tripwire. The Phase 4FN/4FO focused
regression passed 21 tests in 33.07 seconds. Ruff and mypy passed for the phase-owned files.

## Rejected alternatives

- Semantic-version range inference was rejected because wire compatibility cannot be proven from
  version ordering.
- Treating unknown versions as legacy-compatible was rejected as unsafe.
- Loading the matrix from the production database was rejected to preserve artifact-only reads.

## Removal and next dependency

Remove the module, focused test, and this report; no runtime rollback is needed. Phase 4FP should
define monotonic watermark semantics for the schema pair admitted by this matrix.
