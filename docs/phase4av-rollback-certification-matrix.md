# Phase 4AV — Rollback certification matrix

Phase 4AV exercises eleven failure paths against fresh copies of a marker-gated disposable SQLite
template. Input, authorization, expiration, path-isolation, and artifact-publication failures are
certified as pre-mutation refusals. Contention, zero-row, multi-row, postcondition, and simulated
crashes after transaction begin or update are certified as transactional rollbacks.

Each case starts from a deterministic logical snapshot and must finish with the identical hash.
The input template itself is never mutated; cases are copied to an owned temporary directory or an
explicit disposable test directory. Existing case files are refused rather than overwritten.
Artifact-publication failure is modeled before transaction begin, which is the safe ordering this
certification requires.

Outputs are `phase4av.rollback-certification-matrix.v1` and
`phase4av.rollback-proof.v1`. They bind every scenario row and explicitly deny production/research
mutation, production locks, service control, exchange requests, order creation, and execution
authority. The CLI accepts only a disposable template and contains no production or research path
option.

Focused tests independently exercise all eleven scenarios, matrix completeness, exact logical
preservation, the untouched template, invalid markers, insufficient candidate rows, existing work
files, naive time, unknown scenarios, deterministic hashes, and the disposable-only static surface.

