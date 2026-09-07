# Phase 4AM — Property-based transaction invariant model

Phase 4AM is a deterministic, pure in-memory model of the Phase 4AL disposable transaction
protocol. It does not open SQLite, inspect production data, execute SQL, or expose an executor.

## Deterministic generation

The model accepts one or more unique integer seeds, a bounded number of cases per seed, and an
explicit timezone-aware evaluation time. Seed order is canonicalized. Each `(seed, case index)` pair
uses an isolated deterministic pseudorandom stream and one of the fixed scenario classes, making
failures directly reproducible.

The scenario corpus covers:

- valid YES and NO result encodings, single-row and multi-row atomic transactions;
- already-settled state, invalid result values and types;
- duplicate identities, missing columns, malformed rows, and lineage drift;
- timezone-aware, naive, malformed, before-boundary, exact-boundary, and after-boundary times;
- zero-row and multi-row compare-and-swap outcomes; and
- postcondition failure with complete rollback.

The model uses `now >= expires_at` as the exact expiration rule. The one-microsecond after-boundary
case remains eligible; equality and earlier expiration are refused.

## Invariants and shrinking

Every modeled result is checked for legal state-machine transitions, exact per-candidate row count,
precondition-gated commit, timestamp-only changes, refusal preservation, rollback preservation, and
multi-row atomicity. Invalid inputs terminate at `REFUSED`; transaction failures terminate at
`ROLLED_BACK`; only fully verified cases reach `COMMITTED`.

If an invariant fails, a deterministic shrinker removes extra rows and restores irrelevant control
fields in a fixed lexical order while retaining the failure. The counterexample manifest records the
seed, case index, original hash, minimal case, minimal-case hash, and invariant failure codes. Phase
advancement is false whenever the manifest is non-empty.

## Artifacts

Atomic paired publication creates:

- `phase4am.invariant-coverage.v1`; and
- `phase4am.counterexample-manifest.v1`.

The coverage artifact includes sorted seeds, scenario counts, invariant names, a root over every
generated case, and the model version. The manifest binds the coverage hash and canonical
counterexample list. Both are hash protected and non-authorizing. Existing files are refused unless
`--replace` is explicit; replacement restores the previous pair on any publication failure.

## Example

```text
python scripts/local/phase4am_property_invariant_model.py \
  --seed 41 --seed 73 \
  --cases-per-seed 180 \
  --evaluation-time 2026-08-25T12:00:00+00:00 \
  --coverage-output <phase4am-coverage.json> \
  --counterexample-output <phase4am-counterexamples.json>
```

The case limit is 10,000 per seed. Invalid, duplicate, empty, or unbounded configurations fail
closed with stable `PHASE4AM_` errors. A clean run certifies only the pure transaction model; it is
not production execution authorization and does not make Phase 4AL a production executor.
