# Phase 4AT — Structurally disposable-only sandbox executor

Phase 4AT implements exactly one settlement operation against a marked disposable SQLite
simulation database: `SET_CANONICAL_SETTLED_AT_IF_NULL`. It consumes hash-validated Phase 4AK,
4AL, and 4AQ artifacts and an external protected-identity manifest. It has no production or
research database command-line override and no runtime service integration.

The protected manifest must identify both production and research paths by resolved path, device,
inode, size, and nanosecond mtime. The sandbox must have a different path and file identity and a
different parent directory. Those identities are verified before and after the disposable
transaction; the executor never opens a protected database. The disposable database must contain
the Phase 4AL marker.

Execution additionally requires one `READY` Phase 4AK row, a committed and correctly linked Phase
4AL simulation, and a valid unexpired one-attempt Phase 4AQ validation bound to the readiness,
simulation, production identity, and executor build. Authorization expiration is exclusive: an
attempt at the exact expiration instant is refused. Every input remains explicitly non-authorizing
for production.

The transaction uses `BEGIN IMMEDIATE`, revalidates settlement lineage inside the transaction,
and performs a parameterized compare-and-swap update requiring a null timestamp, unchanged result,
and unchanged `updated_at`. Anything other than exactly one affected row rolls back. Successful
output is an atomically published, hash-protected receipt/proof pair. Both artifacts state that only
the disposable simulation was mutated and that production, research, services, locks, exchange,
forecasts, and orders were untouched and unauthorized.

The focused tests cover successful isolation, artifact tampering, invalid state and lineage,
expiration boundaries, direct and hard-link protected aliases, protected metadata drift,
protected-directory placement, invalid markers, build and production-identity bindings, malformed
timestamps, prohibited authorization flags, and the absence of production/research CLI overrides.

