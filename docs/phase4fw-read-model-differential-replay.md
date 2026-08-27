# Phase 4FW — Read-Model Differential Replay

## Outcome

Phase 4FW adds a deterministic, production-read-only comparator for two independently produced
sequences of Phase 4FV provenance-dashboard snapshots. It validates every hash-protected snapshot
before comparison, calculates canonical sequence digests, and reports the first exact divergence.

## Contract and bounds

- Schema identity: `phase4fw-read-model-differential-replay-v1`.
- Inputs are two equal-length, non-empty sequences of validated Phase 4FV models.
- `max_snapshots` is explicit, positive, and enforced at the exact boundary.
- Output is `MATCH` or `DIVERGENCE`, with canonical SHA-256 digests and a hash-protected result.
- `execution_authorized` is permanently false and separately validated.

Malformed, partial, empty, over-bound, length-mismatched, or tampered evidence fails closed before
semantic comparison. The comparator has no database, filesystem, service, network, or exchange
surface and does not publish artifacts.

## Verification

Focused deterministic tests cover equivalent replay, first divergence, empty and partial input,
exact bounds, overflow, length mismatch, malformed input, dashboard tampering, result tampering,
safety-boundary tampering, immutability, and absence of writer methods. Ruff and mypy cover the
phase-owned Python files. Production invariants and writer exclusivity are checked before commit.

## Rejected alternatives

- Comparing only dashboard hashes was rejected because it would not identify the first divergent
  snapshot.
- Accepting partial sequences was rejected because it could turn missing evidence into a match.
- Replaying from the production database was rejected; this contract consumes artifacts only.

## Removal and next dependency

Remove the module, focused test, and this report. No runtime rollback is required. Phase 4FX should
assemble the validated 4FN–4FW contracts into a bounded read-model release candidate.
