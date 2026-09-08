# Phase 4AP — Disposable backup and restore verification

Phase 4AP verifies recoverable SQLite snapshots using only positively marked disposable source,
snapshot, and restore databases. Production and optional research databases are identity guards;
they are never opened for write and are never restore targets.

## Path and identity rules

The source, snapshot, restore, production, and optional research paths must resolve to distinct
locations. Existing files must also have distinct device/inode identities. Snapshot or restore
placement in the same directory as a protected database is refused. Existing snapshot and restore
files are refused, preventing implicit overwrite.

The source is opened with SQLite URI `mode=ro`, `PRAGMA query_only=ON`, and a required
`phase4al.disposable-simulation-database.v1` marker. Production and research path, device/inode,
size, and modification-time metadata are compared before and after the operation.

## Backup and restore proof

SQLite's backup API copies the read-only disposable source into a new snapshot. The snapshot is
fsynced, assigned a SHA-256 file identity, reopened read-only, marker checked, and verified with
`PRAGMA integrity_check`. A second backup restores that snapshot into a new, distinct disposable
target.

For source, snapshot, and restore, the verifier calculates:

- a full-file SHA-256 identity;
- canonical schema definitions, including indexes and constraints;
- deterministic per-table row counts;
- a logical hash over every non-internal table and ordered row; and
- SQLite integrity status.

Acceptance requires source/restore schema equality, row-count equality, logical-hash equality,
source/snapshot logical equality, and successful restore integrity. Snapshot verification fails
closed on any file-hash mismatch, SQLite corruption, marker failure, or integrity failure.

## Artifacts

The paired outputs are:

- `phase4ap.backup-verification.v1`; and
- `phase4ap.restore-proof.v1`.

They bind the file identities, catalogs, equality proof, path-isolation evidence, protected-database
metadata result, and non-authorization fields. `all_equalities_verified=true` proves only that the
disposable snapshot can be restored into its separate disposable target.

The command contains no option to replace an existing database, restore over production/research,
acquire a production lock, control a service, access an exchange, or authorize execution. Generated
snapshot and restore files remain disposable artifacts under the caller's explicitly supplied safe
directories.
