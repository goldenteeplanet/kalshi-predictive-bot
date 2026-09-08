# Phase 4AW — Replay and idempotency verification

Phase 4AW consumes a hash-protected, append-ordered attempt history without accessing any database.
It tracks attempt identifiers, receipt hashes, operation identities, per-ticker database state, and
envelope generations. A successful or ambiguous operation becomes terminal; every later attempt for
that operation is refused, so a second mutation cannot be admitted.

Exact attempt replay, duplicate identifiers, reused receipts, state discontinuity, superseded
envelopes, malformed success, partial receipt loss, and nonmutating outcomes that claim state changes
all fail closed. Rollback or timeout can be followed by a fresh attempt because no earlier mutation
occurred, but reusing the original attempt identifier remains forbidden. Ambiguous attempts are
terminal even when their receipt is missing.

Outputs are `phase4aw.replay-idempotency-verdict.v1` and
`phase4aw.no-second-mutation-proof.v1`. They are deterministic, hash protected, read-only, and
explicitly non-authorizing. The verifier imports no database client and exposes no production,
service, lock, exchange, forecast, or order capability.

Focused tests cover exact replay, replay after success/rollback/timeout, ambiguous attempts,
duplicate identifiers, superseded envelopes, changed approvals/state, receipt reuse, partial
artifact loss, invalid outcome-state combinations, ordering, tampering, and timezone handling.

