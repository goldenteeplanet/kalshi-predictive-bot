# Phase 4AU — Field-level mutation diff proof

Phase 4AU is a read-only verifier for distinct before/after disposable database snapshots and a
hash-valid Phase 4AT receipt. Both databases are opened through SQLite `mode=ro`, must contain the
Phase 4AL disposable marker, and cannot resolve to the same path or file identity.

The verifier inventories every non-internal schema object, table, column, index, foreign-key
constraint, row, and the SQLite application metadata (`application_id`, `user_version`, and
encoding). It rejects schema, metadata, row-count, unrelated-row, unrelated-table, and additional
field changes. Advancement requires exactly one difference: the target settlement row's
`settled_at` changes from null to the receipt-bound value, with before and after row hashes matching
the Phase 4AT receipt.

Outputs are `phase4au.intended-change-proof.v1` and
`phase4au.unrelated-state-preservation-proof.v1`. Both are deterministic, hash protected,
non-authorizing artifacts. The command has no production database argument and contains no database
mutation, service, lock, exchange, forecast, or order capability.

Focused tests cover the valid one-field transition; tampered receipts; unrelated table, row, target
field, schema, index, and application-metadata changes; row deletion; no change; multiple-field
change; same-file and hard-link snapshots; invalid markers; receipt hash mismatch; timezone
requirements; and the static read-only command surface.

