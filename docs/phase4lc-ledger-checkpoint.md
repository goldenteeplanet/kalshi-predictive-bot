# Phase 4LC — Evidence Ledger Checkpoint and Recovery Proof

## Outcome

Phase 4LC adds an exact-chain checkpoint for the Phase 4LB evidence ledger. The checkpoint binds the
complete ledger hash, count, endpoints, tip receipt, canonical creation time, and a strict
`EXACT_FULL_CHAIN_ONLY` recovery policy.

## Refusal coverage

Recovery refuses truncated valid prefixes, extensions, reordered or altered receipts, broken ledger
chains, count and endpoint mismatch, tip rollback, malformed or predating timestamps, checkpoint
tampering, malformed input, and any weaker recovery policy.

## Safety and removal

The implementation reads JSON and validates in memory. It has no database, network, service,
writer-lock, artifact-publication, or trading capability. Remove the script, test, and report to
roll back.

## Next phase

Phase 4LD — Evidence chain independent recomputation audit.
