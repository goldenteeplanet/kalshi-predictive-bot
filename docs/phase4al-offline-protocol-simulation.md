# Phase 4AL — Offline executor protocol simulation

Phase 4AL models the Phase 4AK handoff against a positively identified disposable SQLite
database. It is a simulation protocol, not a production executor, and its outputs never authorize
production settlement execution.

## Inputs and binding

The simulator accepts the hash-valid Phase 4AK readiness envelope and executor-design handoff,
an explicit timezone-aware evaluation time, the production database path for read-only identity
verification, and a separate disposable database path. It verifies:

- the Phase 4AK publication-pair and artifact hashes;
- the readiness-row hash aggregate and non-authorization fields;
- exact production path, size, and modification-time binding from Phase 4AK;
- production access through SQLite URI `mode=ro` with `PRAGMA query_only=ON`;
- path, parent-directory, and device/inode separation;
- the `phase4al.disposable-simulation-database.v1` marker in the disposable database; and
- duplicate candidate, readiness, and exact expiration-boundary refusal rules.

The production database is never attached to the disposable connection. No writer lock, service
control, exchange client, order interface, or production mutation callback exists in this tool.

## Disposable transaction protocol

For ready rows, the simulator performs this fixed state machine:

1. Capture full logical and unrelated-state hashes.
2. Validate the initial preconditions.
3. Start `BEGIN IMMEDIATE` on the disposable database only.
4. Revalidate each settlement row and its lineage hash inside the transaction.
5. Apply a parameterized compare-and-swap to the canonical `settled_at` field.
6. Require exactly one affected row for every candidate.
7. Verify the intended timestamp and unrelated-state preservation.
8. Commit the disposable transaction, or roll it back on any failure.

Deterministic failure injection covers every boundary from before transaction start through the
instant before commit. Failure reports are emitted only after the complete disposable logical state
matches its initial hash.

## Outputs

Atomic paired publication produces:

- `phase4al.offline-protocol-simulation-report.v1`; and
- `phase4al.protocol-safety-proof.v1`.

The pair binds the Phase 4AK hashes, readiness-row hashes, initial/final logical hashes, stable
disposable identity, stage results, rollback/commit proof, path-isolation proof, and production
before/after identity. Canonical JSON and sorted fields make identical starting state, inputs,
evaluation time, and failure stage byte reproducible.

`database_mutation_performed=false` refers to protected production/research data. The separate
`disposable_simulation_mutation_performed` field explicitly records whether the disposable clone
committed the simulated semantic change. Every artifact keeps production/research mutation,
production lock, service control, exchange request, order creation, and execution authorization
false.

## Command

```text
python scripts/local/phase4al_offline_protocol_simulation.py \
  --production-db <read-only-production.db> \
  --simulation-db <positively-marked-disposable.db> \
  --phase4ak-readiness <phase4ak-readiness.json> \
  --phase4ak-handoff <phase4ak-handoff.json> \
  --evaluation-time 2026-08-25T00:00:00+00:00 \
  --simulation-report-output <phase4al-report.json> \
  --safety-proof-output <phase4al-proof.json>
```

Existing outputs are refused unless `--replace` is explicit. Replacement backs up both existing
files, atomically installs the new pair, restores the prior pair on failure, fsyncs files and the
output directory where supported, and removes temporary/backup files.

## Operator interpretation

`SIMULATION_COMMITTED` means only that the disposable protocol satisfied its modeled invariants.
It is not evidence of human approval, production authorization, or production readiness. A refusal
or rollback reason beginning with `PHASE4AL_` is fail-closed and must not be bypassed. Any future
scope expansion still requires separate user authority and a separately reviewed production design.
