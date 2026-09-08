# Phase 4LI — Evidence Corpus Mutation-Coverage and Blind-Spot Audit

## Outcome

Phase 4LI statically discovers Phase 4LG refusal literals and maps each executable branch to a
deterministic probe. It also verifies complete Phase 4LH mutation-kind coverage, exact at/over-limit
behavior, stable seed and corpus identity, accepted-input coverage, and failure-preserving minimizer
coverage.

Defensive branches unreachable through Python's JSON grammar are explicitly classified with a
reviewable rationale rather than counted as dynamically exercised.

## Safety and removal

The audit reads Python source through introspection and processes bounded in-memory fixtures. It has
no database, network, service, lock, artifact-publication, or trading capability. Remove the script,
focused test, and report to roll back.

## Next phase

Phase 4LJ — Evidence safety workstream certification.
