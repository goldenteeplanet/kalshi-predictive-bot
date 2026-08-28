# Phase 4LD — Independent Evidence-Chain Recomputation Audit

## Outcome

Phase 4LD independently recomputes Phase 4LB receipt hashes and chain semantics, verifies each
receipt against actual read-only Git commit, parent, tree, and path evidence, reconstructs the
claimed ledger identity, and cross-checks the Phase 4LC anti-rollback checkpoint.

## Independence and refusal coverage

The implementation does not call the Phase 4LB validator. It refuses implementation disagreement,
missing or duplicate commits, altered receipts, parent/tree/path mismatch, unsafe paths,
non-contiguous phases, broken receipt links, truncated chains, checkpoint rollback, and checkpoint
tampering.

## Safety and removal

Only read-only Git operations and in-memory JSON are used. Tests create disposable repositories.
There is no production database, network, service, lock, artifact-publication, or trading
capability. Remove the script, test, and report to roll back.

## Next phase

Phase 4LE — Evidence schema forward-compatibility envelope.
