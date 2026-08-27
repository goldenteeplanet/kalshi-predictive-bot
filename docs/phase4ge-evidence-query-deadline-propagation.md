# Phase 4GE — Evidence Query Deadline Propagation

## Outcome and measured evidence

Phase 4GE deterministically propagates an armed Phase 4GD relative deadline to a bounded list of
named stages. Focused tests cover valid propagation, empty and partial inputs, stage limits, exact
freshness and remaining-time boundaries, stale and expired states, malformed and duplicate stages,
tampering, retry-contract enforcement, and absence of clocks, queries, or mutations. Ruff and pytest
evidence is reproducible from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4ge-evidence-query-deadline-propagation-v1`.
- At most eight unique, nonempty stages are accepted by default.
- Remaining time is calculated once as `max(0, cancel_after - elapsed - overhead)` and propagated
  unchanged to every stage, preventing downstream deadline extension.
- Boundary evidence exactly 300 seconds old and one remaining millisecond are eligible by default;
  one second older is stale and zero remaining milliseconds is expired.
- The output binds the 4GD boundary, source lineage, supplied age/elapsed/overhead, every stage
  envelope, zero retries, and `execution_authorized=false` under canonical SHA-256.

## Safety, alternatives, rollback, and next dependency

This module consumes supplied relative timing values and does not inspect a clock, dispatch work,
cancel a query, open SQLite, publish artifacts, control services, or contact an exchange. Per-stage
deadline resets and automatic retries were rejected because either could exceed the parent budget.
Default-path UI integration was rejected because deadline construction is an opt-in control-plane
operation.

Rollback is deletion of the module, focused test, and report, with no runtime action. Phase 4GF
should validate downstream stage acknowledgements against this propagation artifact without running
the query.
