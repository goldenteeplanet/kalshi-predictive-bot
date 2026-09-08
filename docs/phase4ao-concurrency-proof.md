# Phase 4AO — Concurrent contention and lost-update proof

Phase 4AO uses two independent SQLite connections and controlled interleavings against copied,
positively marked disposable databases. It proves that the Phase 4AL compare-and-swap shape permits
at most one successful timestamp mutation for a settlement identity.

## Scenario matrix

The deterministic campaign exercises:

- two valid candidates for the same row;
- a duplicated attempt identifier refused before a second CAS;
- a stale reader followed by a stale CAS;
- conflicting proposed timestamps;
- an explicit disposable `BEGIN IMMEDIATE` lock contention;
- zero-timeout busy refusal;
- a controlled read/commit/retry interleaving;
- replay after a commit response is intentionally obscured; and
- a direct lost-update attempt.

The lock holder and contender are separate connections. Busy conditions are recorded as refusal
events, not retried implicitly. Where the scenario includes an explicit retry, the retry runs the
same `settled_at IS NULL` compare-and-swap and must affect zero rows.

## Isolation and proof

Every scenario copies a database bearing the Phase 4AL disposable marker into a unique temporary
workspace. Production/template path overlap, shared parent, hard-link identity, and a work root in
the production directory tree are refused. Production identity metadata is compared before and
after each scenario.

The harness hashes all tables while masking only the intended target `settled_at` field. A scenario
passes only when:

- exactly one compare-and-swap succeeds;
- the first canonical timestamp is the final value;
- every stale, conflicting, duplicate, timeout, or replay attempt causes no second mutation;
- unrelated state remains hash-identical; and
- production metadata remains identical.

The scenario workspace is removed after classification. The harness does not use threads with
timing races; the event schedule is explicit and reproducible while the database locking and row
counts are supplied by SQLite itself.

## Artifacts

Phase 4AO produces:

- `phase4ao.concurrent-contention-report.v1`; and
- `phase4ao.lost-update-proof.v1`.

The report contains the canonical event stream and outcome for every scenario. The proof binds the
report hash, scenario count, maximum successful CAS count, and unrelated-state preservation result.
`LOST_UPDATE_AND_DUPLICATE_MUTATION_PREVENTED` certifies only the disposable harness.

Neither artifact contains executable SQL, a production path override, service controls, exchange
interfaces, order entry, or execution authorization. No production lock is acquired.
