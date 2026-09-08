# Phase 4GC — SQLite Busy-Timeout Evidence

## Outcome and measured evidence

Phase 4GC verifies supplied, hash-protected busy-timeout observations against a granted Phase 4GB
read budget. Focused tests cover verified evidence, empty and partial input, collection bounds, exact
freshness, stale and mismatch states, malformed data, tampering, lineage mismatch, denied budgets,
and absence of SQLite or mutation calls. Ruff and pytest results are reproducible from committed
files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gc-sqlite-busy-timeout-evidence-v1`.
- At most 16 observations are accepted by default, all linked to the budget source identity and
  watermark. Empty, oversized, mixed, malformed, or tampered evidence fails closed.
- The default inclusive evidence-age limit is 300 seconds. Exactly 300 remains eligible; 301 is
  `STALE`.
- `VERIFIED` requires every supplied observation to equal the budget's required zero-millisecond
  busy timeout. Any fresh deviation is an explicit `MISMATCH`.
- Canonical SHA-256 binds the budget, observations, lineage, thresholds, status, and permanent
  `execution_authorized=false` boundary.

## Safety, alternatives, rollback, and next dependency

The implementation consumes in-memory observations only. It imports no SQLite API and cannot run a
pragma, open a database, retry a query, publish an artifact, control a service, or contact an
exchange. Setting `busy_timeout`, auto-retrying locks, and collecting evidence on a default UI path
were rejected because they could change connection behavior or add contention.

Rollback is deletion of the module, focused test, and report; no runtime action is needed. Phase
4GD should combine the 4GB budget and 4GC evidence into a deterministic fail-fast read policy while
remaining non-executable.
