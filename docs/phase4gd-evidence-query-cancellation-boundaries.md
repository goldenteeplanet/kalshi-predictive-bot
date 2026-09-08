# Phase 4GD — Evidence Query Cancellation Boundaries

## Outcome and measured evidence

Phase 4GD composes a granted Phase 4GB read budget and Phase 4GC busy-timeout evidence into a
deterministic, non-executable cancellation envelope. Focused tests cover arming, empty and partial
inputs, exact limits, deadline/interval/callback breaches, stale evidence, malformed fields,
tampering, cross-link failures, retry-contract tampering, and absence of query or mutation calls.
Ruff and pytest evidence is reproducible from committed files.

## Contract, freshness, provenance, and bounds

- Schema: `phase4gd-evidence-query-cancellation-boundaries-v1`.
- `ARM` requires a granted budget, verified timeout evidence, exact budget/evidence lineage, a
  positive deadline no greater than the budget duration, and a progress interval no greater than
  the deadline.
- Progress callbacks are calculated deterministically with ceiling division and capped at 128 by
  default. Exact duration and callback limits remain eligible; one-unit breaches deny.
- Stale or mismatched timeout evidence produces an explicit `DENY`; malformed, partial, tampered,
  or unlinked evidence fails closed.
- Retry count is permanently zero. Canonical SHA-256 binds all upstream hashes, lineage, limits,
  decision, reasons, and `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

This module defines boundaries only. It cannot register a SQLite progress handler, execute or cancel
a query, retry work, publish an artifact, control a service, or contact an exchange. Automatic retry,
thread interruption, and default-path UI computation were rejected because they add runtime effects
or hide unavailable evidence.

Rollback is deletion of the module, focused test, and report with no runtime action. Phase 4GE should
independently verify cancellation observations against this envelope before any executor is proposed.
