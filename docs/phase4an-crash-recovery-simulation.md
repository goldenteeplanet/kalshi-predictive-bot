# Phase 4AN — Crash-consistency and recovery simulation

Phase 4AN exercises database and artifact persistence boundaries in isolated disposable
workspaces. It uses deterministic injected termination exceptions; it does not terminate processes,
control services, acquire production locks, or provide production recovery commands.

## Isolation

Each scenario begins from a copied SQLite template bearing the
`phase4al.disposable-simulation-database.v1` marker. The production and template paths, parents,
and device/inode identities must be distinct. The disposable work root may not be the production
directory or a descendant of it. Production identity metadata is captured before and after each
scenario and must remain identical.

The scenario copy is created in an exact temporary directory below the supplied disposable root and
is removed in a `finally` block after classification. Only that generated directory is removed.

## Persistence boundaries

The campaign injects a crash:

- before transaction start;
- during the transaction;
- after mutation but before commit;
- after disposable commit but before receipt publication;
- during durable temporary-file publication;
- between paired receipt replacements; and
- while modeling backup/restoration after the first new file is installed.

Pre-commit termination must reopen as the exact pre-transaction logical row. Post-commit
termination must reopen as the exact intended committed row. Any third database state fails closed
as `PHASE4AN_DATABASE_STATE_UNCLASSIFIABLE`.

## Classification and recovery

Receipt state is classified independently as `ABSENT`, `VALID_PAIR`, or
`PARTIAL_OR_INVALID`. Pair validation recomputes both hashes, publication-pair identity, and receipt
lineage. Recovery removes only the simulated receipt files in the scenario directory:

- a pre-transaction database retains no receipt; or
- an intended committed database gets a deterministically reconstructed, hash-valid simulation
  receipt pair.

The recovery is accepted only when database and receipt states agree. The after-commit/missing-
receipt case therefore becomes `COMMIT_AND_RECEIPT_RECOVERED`, while pre-commit failures become
`ROLLBACK_OR_PREMUTATION_REFUSAL_RECOVERED`.

## Artifacts

The campaign publishes:

- `phase4an.crash-consistency-report.v1`; and
- `phase4an.recovery-proof-manifest.v1`.

The report records every required boundary, before/after state hashes, crash-time classification,
post-recovery classification, and production metadata result. The proof binds the report and counts
every required verified boundary. `ALL_PERSISTENCE_BOUNDARIES_RECOVERABLE` applies only to the
disposable protocol.

No artifact contains production recovery commands, executable SQL, service controls, credentials,
or authorization. Disposable receipt recovery cannot authorize production execution and does not
prove that a production executor exists.
