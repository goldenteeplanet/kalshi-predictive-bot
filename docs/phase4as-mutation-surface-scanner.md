# Phase 4AS — Static mutation-surface scanner

Phase 4AS scans selected repository globs without importing or executing source. The checked,
hash-protected allowlist scopes the guarded Phase 4A/4B-series tooling and its tests while the scanner
itself remains reusable for arbitrary repository-relative globs.

The AST/text scanners detect writable SQLite opens, settlement UPDATE/INSERT/DELETE literals,
session ORM mutations, dynamic database execution in settlement-aware modules, Alembic migration
operations, service scripts, writer locks, subprocess mutation commands, and mutation-capable main
entrypoints. Invalid source, oversized files, excessive file counts, malformed globs, and tampered
allowlists fail closed.

Test paths receive the built-in `TEST` classification. Every non-test finding needs an exact
path/capability allowlist entry whose scope is `DISPOSABLE_SIMULATION` or `ISOLATED_RESEARCH` and
whose non-empty justification is hash represented in the inventory. Production scope is not a valid
allowlist value. Unused entries are treated as stale and fail advancement.

The reviewed allowlist is `config/phase4as-mutation-surface-allowlist.json`. Its real-repository
validation scans Phase 4A/4B scripts and tests, classifies read-only dynamic audit queries and the
marker-gated disposable simulations, including the Phase 4BG in-memory rollback measurement, and
leaves no unexplained or stale finding.

Outputs are `phase4as.mutation-surface-inventory.v1` and
`phase4as.mutation-surface-verdict.v1`. `MUTATION_SURFACES_EXPLAINED` means only that all findings in
the declared scan scope have safe classifications; it grants no execution authority and performs no
database, service, lock, exchange, forecast, or order action.
