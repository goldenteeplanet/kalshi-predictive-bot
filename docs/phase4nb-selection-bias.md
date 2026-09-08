# Phase 4NB — Multiple Testing and Strategy-Selection Bias

## Outcome

Phase 4NB registers every strategy, parameter, feature family, threshold, subset, seed, metric, and
evaluation attempt, including failed and abandoned work. It calculates deterministic Bonferroni,
Holm, Benjamini–Hochberg, and effect-deflation controls over the complete declared search history.

## Selection boundary

Missing attempts, duplicate semantic trials, post-hoc metric switching, seed hunting, subset mining,
and grid expansion cannot disappear from the correction denominator. Nested selection permits one
untouched final holdout evaluation of the exact inner-selected strategy. Peeking, reuse, repeated
holdouts, and post-selection substitution refuse.

## Reproducible evidence

- focused Phase 4NA–4NB suite: 21 passed
- registered grid trials: 20
- cherry-picked raw p-value: `0.01`
- Bonferroni-corrected p-value: `0.2`
- survives all corrections: false
- complete selection-history SHA-256: `c2497dad5106ea39110f6c0277ea4fd08d49bd90c7da639276494a684f5893ae`
- correction audit SHA-256: `d6b6ae4c320c38fb81928fbe7a2b5a3ecce65cfb92e0e955e83cee6c7bffda4c`
- valid one-shot nested holdout SHA-256: `1e51853a111d3010bbe9145505ae2b89ecc034ce624ed694d9a90b50811b15e4`

## Safety and removal

The audit is offline, deterministic, and non-persistent, with no network, runtime, paper, demo,
live, autopilot, or order capability. Remove the three phase files to roll back.

## Next phase

Phase 4NC — Probability calibration stress, reliability decomposition, and tail-bin proof.
