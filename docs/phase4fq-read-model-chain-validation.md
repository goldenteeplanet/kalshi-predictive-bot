# Phase 4FQ — Read-Model Chain Validation

## Outcome

Phase 4FQ adds deterministic validation for a bounded chronological chain of read-model
observations. Each node binds its ordinal, timestamp, source identity, sequence, payload hash, and
previous-node hash into a canonical SHA-256 identity.

## Contract

- Schema: `phase4fq-read-model-chain-v1`.
- Genesis: ordinal zero with no previous-node hash.
- Continuity: every later ordinal is contiguous and references the immediately preceding node.
- Chronology: timestamps increase strictly.
- Provenance: source and source identity remain constant across the chain.
- Progress: sequences are monotonic; equal values are valid dwell, regression is invalid.
- Uniqueness: a payload hash may appear only once.
- Freshness: head age is strictly below the configured threshold.
- Bounds: node count is limited before traversal.
- Output: a frozen summary with genesis/head hashes, range, progress, and head age.

Any missing field, unsupported schema, malformed hash or timestamp, duplicate payload, broken link,
out-of-order observation, identity change, regression, stale/future head, or bound violation fails
the entire chain closed.

## Safety and verification

The implementation consumes caller-supplied in-memory artifacts only. It has no database,
network, exchange, service-control, filesystem-write, or mutation interface. Deterministic tests
cover valid chains, empty/malformed input, exact node and staleness boundaries, tampering, missing
links, ordinal discontinuity, duplicates, regression, chronology, identity change, partial nodes,
future heads, input immutability, and absence of writer methods. The cumulative Phase 4FN–4FQ
regression passed 40 tests in 85.49 seconds. Ruff and mypy passed for the phase-owned files.

## Rejected alternatives

- Sorting supplied nodes was rejected because it would conceal publication-order defects.
- Skipping missing ordinals was rejected because it destroys completeness evidence.
- Trusting only the head hash was rejected because every intermediate link must be verified.

## Removal and next dependency

Remove the module, test, and report; no runtime rollback is required. Phase 4FR should define a
retention policy that removes only safe prefixes while preserving a verifiable chain anchor.
