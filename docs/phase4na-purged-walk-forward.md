# Phase 4NA — Purged Walk-Forward and Embargo Proof

## Outcome

Phase 4NA creates deterministic expanding or rolling walk-forward folds grouped by related event.
Training rows must end and become available before the embargo cutoff, cannot overlap any test label
window, and cannot share the test event group. Normalization, class summaries, thresholds, and
predictions are fitted independently inside each ready fold.

## Leakage comparison

An explicit ordinary-cross-validation control uses post-outcome revision values to quantify the
performance inflation that a leaky evaluation can create. It is labeled as a deliberate control and
is never accepted as valid evidence. Sparse folds remain sparse rather than fabricating samples.

## Reproducible evidence

- focused Phase 4MZ–4NA suite: 20 passed
- ready embargoed folds: 3
- fold-set SHA-256: `45896b5e00c0293c8b279a3830b5410559a2e964b9e4597e40386dca05627f61`
- walk-forward evaluation SHA-256: `67192a92ddddf30603b9dc53272f7ff89c6f2576f979a1d2d2bee533faef7999`
- leakage audit SHA-256: `c1fb8695ea2a6fbe81bfaeb746f2ed72fea3d942f0be3b4f90f9928fce0815b4`
- purged Brier score: `0.3511164237289916`
- deliberately leaky ordinary-CV Brier score: `0.07124031965876075`
- measured leakage inflation: `0.27987610407023084`
- comparison SHA-256: `29bd3ce2670a34bb40c74a6ed512ea27d8b209ee1b03ae5858b87f22cc7df9e5`

## Safety and removal

All splitting and evaluation is offline, deterministic, and non-persistent. It has no database,
network, runtime, paper, demo, live, autopilot, or order capability. Remove the three phase files to
roll back.

## Next phase

Phase 4NB — Multiple-testing correction and strategy-selection-bias proof.
