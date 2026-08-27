# Phase 4FT — Read-Model Concurrent-Reader Audit

## Outcome

Phase 4FT runs bounded real-thread readers while a single simulation writer atomically publishes
successive artifacts inside a marked Phase 4FS temporary workspace. It proves each completed read
observes a whole hash-valid artifact, each reader's sequence is monotonic, and the final head equals
the declared final publication.

## Contract

- Inputs: marked simulation workspace, strictly increasing sequences, reader/read counts and upper
  bounds, byte bound, fresh evidence timestamp, and trusted current time.
- Output: frozen verdict, deterministic reader ordering, total reads, initial/final sequences,
  per-reader traces, and sorted violations.
- Freshness: evidence age must be strictly below the configured maximum; equality is stale.
- Bounds: positive reader/read limits are enforced before threads start.
- Failure: any reader exception, invalid current state, unexpected sequence, regression, short read
  trace, missing reader, or wrong final head produces `FAIL_CLOSED`.

## Safety and verification

The only writer is the Phase 4FS simulation publisher, which refuses unmarked/non-simulation
directories. Tests use pytest temporary paths and cover successful concurrent reads, exact bounds,
bound overflow, empty/out-of-order publication, exact staleness, future evidence, missing markers,
injected reader failure, and sibling-file isolation. No production database or artifact is opened;
no service or exchange control exists. The cumulative Phase 4FN–4FT regression passed 65 tests in
49.11 seconds. Ruff and mypy passed for the phase-owned files.

## Rejected alternatives

- Mock-only concurrency was rejected because it cannot exercise atomic replacement during reads.
- Retrying failed readers was rejected because it could hide partial failure evidence.
- Reordering traces by completion was rejected; results are normalized by stable reader identity.

## Removal and next dependency

Remove the module, focused test, and report. Temporary test workspaces are disposable. Phase 4FU
should classify fresh, warning, stale, and stalled read-model evidence with explicit escalation
thresholds.
