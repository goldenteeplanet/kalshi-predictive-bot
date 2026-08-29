# Phase 4ON — Recovery-Tail Regression Baselines

## Outcome

Phase 4ON creates hash-bound, versioned baselines only from independently verified recovery soaks.
The comparison gate recomputes absolute and relative deltas for p95, p99, and worst cost, applies
explicit tolerances, and classifies each metric as improvement, within tolerance, or regression.

Promotion requires a passing candidate with a different seed, unchanged sample size, zero unsafe
runs, no tail-budget violation, a trusted baseline hash, and no metric beyond tolerance. Baseline or
candidate tampering, seed reuse, sample manipulation, invalid tolerance policy, and unsafe evidence
fail closed. Promotion creates the next version without modifying runtime state.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OM/4ON regression: `12 passed in 29.04s`.
- Version 1 baseline SHA-256:
  `f0703fd7cac7821ce52bb3583511c010992a6e02223bdb3568828577c2d04245`.
- Candidate comparison: `PASS`; p95/worst within tolerance and p99 improved.
- Comparison SHA-256: `78c562eb6c98cbe281b83c542d5d9390147032ed292619dc0d18ff1083e9752c`.
- Promoted version: `2`; baseline SHA-256:
  `64f3879ab7e026a3cc8abfdc217fca45935ddec0be8d21125273ffca7fb190b1`.

## Safety and removal

All comparison and promotion artifacts are offline, in-memory, and non-persistent. They cannot
create paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4ON files
to roll back.

## Next phase

Phase 4OO — Recovery-baseline lineage, rollback, and multi-version retention proof.
