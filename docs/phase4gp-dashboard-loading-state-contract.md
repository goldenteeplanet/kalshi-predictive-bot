# Phase 4GP — Dashboard Loading-State Contract

## Outcome and measured evidence

Phase 4GP maps Phase 4GO disclosure modes and caller-supplied observations into explicit panel states:
`DEFERRED`, `UNAVAILABLE`, `PENDING`, `LOADING`, `READY`, `ERROR`, and `TIMED_OUT`. Empty successful
results remain `READY` with count zero instead of being confused with missing evidence. Focused tests
cover deterministic order, empty and partial inputs, observation and timeout boundaries, staleness,
failure, empty results, malformed observations, forbidden deferred activity, lineage, and tampering.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gp-dashboard-loading-state-contract-v1`.
- Observations bind panel ID, load state, elapsed milliseconds, optional result count, lineage, and age.
- The observation set must exactly match the Phase 4GO disclosure plan; the default maximum is 32.
- Loading at exactly 2,000 ms remains `LOADING`; one millisecond more is `TIMED_OUT`.
- Evidence exactly 300 seconds old remains eligible; older evidence is `STALE` and all panels become
  `UNAVAILABLE`. Deferred panels must remain `NOT_STARTED`, proving no hidden query occurred.
- Canonical SHA-256 binds observations and output; `read_only=true` and
  `execution_authorized=false` are immutable.

## Safety, rejected alternatives, rollback, and next dependency

The contract performs no queries, HTTP calls, writes, service control, or execution action. Inferring
success from an absent response and silently retrying timed-out panels were rejected because both hide
latency and can amplify load.

Rollback is deletion of this module, focused test, and report. Phase 4GQ should define retry semantics
that consume `ERROR` and `TIMED_OUT` states while keeping attempts explicit and bounded.
