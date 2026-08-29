# Phase 4MJ — Recovery-Evidence Workstream Certification and Residual-Risk Gate

## Outcome

Phase 4MJ certifies the committed Phase 4LK–4MI recovery and evidence workstream against exact
introducing commits, linear ancestry, 75 phase-owned tracked paths, index/worktree blob identity,
versioned schemas, focused-test coverage, capability scans, and the canonical fail-closed invariant
snapshot. Every one of the 25 phases must independently pass.

## Refusal and residual risk

The gate refuses missing or dirty paths, stale evidence hashes, phase/test gaps, ancestry or head
substitution, missing schema versions, forbidden operational capabilities, and weakened invariants.
It explicitly retains risks for absent production repair execution and persistence, external key
custody, separately supervised WSL recovery, and settlement-dependent validation.

## Reproducible evidence

- Certified phases: 25 of 25, with 75 exact owned paths.
- Focused workstream tests: 378 passed in 183.85 seconds.
- Test-evidence SHA-256:
  `3e0db6ca0db4a3859ace88a225b6f58f6b9d2a8cad0704e2f85ded2f6135fc81`
- Certified range head: `1d7e9cf073ec0fcb2ff1662b6ee0e208fc83aac0` (Phase 4MI).
- Certification SHA-256:
  `4c63a89ebe05898d918cf2e8990c7e4bdf7d631614f9b40564905a89e7814f6b`

The range head is pinned independently of repository `HEAD`, allowing this certification commit and
later descendants without weakening or making the Phase 4LK–4MI certificate self-invalidating.

## Safety and removal

Certification uses only local file and Git reads. It does not modify runtime state, persist
artifacts, compact production data, execute repairs, control WSL or services, access the network, or
create any order. Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4MK — Recovery-workstream independent reproducibility and clean-checkout audit.
