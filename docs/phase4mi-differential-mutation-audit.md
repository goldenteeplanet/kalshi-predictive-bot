# Phase 4MI — Differential-Certification Mutation Coverage and Minimization

## Outcome

Phase 4MI runs a deterministic bounded mutation corpus over authoritative records, rehashed record
semantics, retained suffixes and cut points, snapshot bindings, and migrated-artifact bindings. A
mutation is covered only when it is refused or produces a localized divergence; silent semantic
equivalence fails certification.

## Counterexample minimization

Suffix failures are greedily minimized without mutating their source and only while preserving the
same normalized refusal or first-divergent-field signature. The report hash-binds the full corpus,
surface coverage, and minimized counterexamples.

## Reproducible evidence

- Detected mutations: 54 of 54 across all five required surfaces.
- Corpus SHA-256: `6faeafa6b4a435fb3d45001a23dd51a093b21a4b14edab5b5ced41972df8b41f`
- Minimization SHA-256:
  `338031c93b6d8df60a598bc4983b13db4d211b2edcefa8dc5f13f13132880ddc`
- Complete audit SHA-256:
  `271f1e5ab4c3c965e592798a6348d97d97bef6e9635a5033bb7bd2ed35b7c823`

The audit also closed a discovered restoration gap by externally pinning the prior-snapshot anchor;
single-record reversal is excluded as a proven byte-identical no-op rather than counted as a
semantic mutation.

## Safety and removal

The corpus is capped at 128 mutations and runs entirely in memory. It cannot persist artifacts,
compact production data, execute repairs, change runtime state, control WSL or services, access the
network, or create any order. Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4MJ — Recovery evidence workstream certification and residual-risk gate.
