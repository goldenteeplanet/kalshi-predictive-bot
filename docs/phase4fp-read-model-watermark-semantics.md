# Phase 4FP — Read-Model Watermark Semantics

## Outcome

Phase 4FP defines deterministic, hash-protected watermark observations and a bounded monotonic
transition evaluator. It distinguishes initial evidence, unchanged evidence, and actual progress
without treating a reset, source change, identity change, future timestamp, or unexplained jump as
valid advancement.

## Contract

| Property | Rule |
|---|---|
| Schema | `phase4fp-read-model-watermark-v1` |
| Canonical watermark | `<source>:<non-negative integer sequence>` |
| Provenance | Mandatory SHA-256 source identity |
| Time | UTC-normalized aware observation timestamp |
| Integrity | Canonical SHA-256 over every non-hash field |
| Freshness | Current age is strictly less than the configured maximum |
| Monotonicity | Sequence may remain equal or increase; regression fails closed |
| Forward bound | Increase cannot exceed caller-supplied maximum step |
| Continuity | Source and source identity must remain identical |
| Output | Frozen `INITIAL`, `UNCHANGED`, or `PROGRESSED` result |

## Safety and resource bounds

The evaluator accepts only in-memory artifact mappings. It has no database, network, exchange,
artifact publication, service control, or mutation imports. Validation is constant-space beyond
the two bounded mappings and performs no retry or fallback query.

## Verification evidence

Deterministic tests cover initial, progress, unchanged, empty/partial, exact staleness, tampering,
malformed canonical form, regression, maximum-step overflow, source and identity change,
out-of-order and future timestamps, input immutability, and absence of writer methods. Measured
coverage across Phases 4FN–4FP passed 31 tests in 40.47 seconds. Ruff and mypy passed for the
phase-owned files.

## Rejected alternatives

- Wall-clock timestamps alone were rejected because equal timestamps cannot prove source progress.
- Accepting sequence resets on source identity change was rejected because it hides discontinuity.
- Unlimited forward jumps were rejected because they can conceal missing evidence.

## Removal and next dependency

Remove the module, focused test, and report. No database or runtime rollback is needed. Phase 4FQ
should bind successive admitted read-model artifacts into a validated hash and watermark chain.
