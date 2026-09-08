# Phase 4ME — Atomic Nonce-Consumption Ledger and Crash-Consistency Proof

## Outcome

Phase 4ME models nonce consumption as a hash-linked, generation-bound event ledger with `PREPARE`,
`DURABLE_CONSUME`, `COMMIT`, and `ABORT` transitions. Each append carries the expected prior
generation and head hash, making concurrent compare-and-swap conflicts and history damage explicit.

## Crash and replay semantics

A prepare permanently reserves its nonce. Recovery proposes abort for a prepared transaction and
commit for a durably consumed transaction, but neither path makes the token reusable. Exact record
replay is idempotent; conflicting replay, a second nonce owner, invalid transitions, receipt or token
substitution, truncation against an external anchor, reordering, and hash or generation damage fail
closed.

## Reproducible evidence

- Committed-ledger validation SHA-256:
  `aaa22fa496d9e32736db24ad16b9f540d33f55fc22059bf33f96ae66e99e7f2f`
- Prepared-crash recovery-plan SHA-256:
  `cb842256946c29cc63a2ffefa2338aa4d5ba4222a5c95469340784d4bc7a813a`
- Durable-consume crash recovery-plan SHA-256:
  `be207ecb5a5878faa385949c459330b7aadb88966dca3a36925da393b3b942d4`
- Anchored truncation-refusal SHA-256:
  `66affe7b5cd0a78d55d540897e235f7e35fccb470c5e46fb203ba771a61077d1`

## Safety and removal

This is an in-memory, offline simulator. Recovery emits proposed records only. It does not persist
to the production database, execute repairs, change runtime state, control WSL or services, access
the network, or create any order. Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4MF — Nonce-ledger snapshot, compaction, and cryptographic anchor model.
