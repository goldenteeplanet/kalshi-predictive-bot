# Phase 4GB — SQLite Read Transaction Budget

## Outcome and measured evidence

Phase 4GB converts one validated Phase 4GA contention audit into a deterministic, non-executable
SQLite read-transaction budget. Focused tests cover a valid grant, empty and partial inputs, exact
boundaries, row/duration/staleness/contention denials, malformed fields, tampering, read-only contract
tampering, and absence of SQLite or mutation calls. Ruff and pytest evidence is reproducible from
the committed implementation and test.

## Contract, freshness, and bounded resources

- Schema: `phase4gb-sqlite-read-transaction-budget-v1`.
- Default limits are one result row, 100 milliseconds, and 300-second evidence age; all are
  inclusive. A one-unit breach deterministically denies the budget.
- A grant requires a `CLEAR` 4GA audit within the independent evidence-age ceiling.
- Every result mandates query-only access, an immutable source, zero busy-wait milliseconds, and
  `execution_authorized=false`. Canonical SHA-256 binds the audit lineage and all limits.
- Empty, malformed, partial, or tampered inputs fail closed; ordinary budget breaches return `DENY`.

## Safety, alternatives, rollback, and next dependency

This capability is a pure in-memory decision and imports no SQLite API. It cannot begin a read
transaction, execute SQL, publish an artifact, control a service, or reach an exchange. A nonzero
busy timeout and automatic retry were rejected because either could extend contention. Applying
SQLite pragmas or indexes was rejected as out of scope and potentially mutating. No UI integration
is useful before a separately approved executor exists.

Rollback is removal of the module, focused test, and report, with no data or runtime action. Phase
4GC should use this budget as immutable input to define a fail-fast settled-count read protocol
without granting production execution authority.
