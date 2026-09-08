# Phase 4FR — Read-Model Retention Policy

## Outcome

Phase 4FR adds a deterministic, non-executable retention planner for validated Phase 4FQ chains.
It identifies only a bounded oldest prefix, keeps the newest requested suffix, and records a
hash-protected boundary anchor connecting the removed head to the first retained node.

## Contract

- Input: a complete validated chain, positive `keep_last`, non-negative `max_remove`, trusted time,
  chain freshness, and node bounds.
- Output: `phase4fr-read-model-retention-plan-v1` with ordered remove/retain hash lists, original
  chain head/count, explicit policy, optional boundary anchor, and canonical plan hash.
- Exact boundary: removing exactly `max_remove` is allowed; exceeding it fails closed.
- No-op: if the chain fits, the removal list is empty and no anchor is invented.
- Anchor: records original genesis, removed head, first retained node, their sequences, and the
  retained node's unchanged previous hash.
- Operation identity: always `PLAN_ONLY_NO_DELETE`.

Validation recomputes the plan from the complete source chain. A plan cannot be reused with a
different chain even if its policy values match.

## Safety and verification

The capability has no delete, unlink, filesystem-write, database, network, exchange, or service
control operation. Tests use in-memory chain fixtures and cover valid safe retention, no-op,
empty/malformed sources, exact removal boundaries, overflow, staleness, chronology, tampering,
partial plans, source-chain substitution, immutability, and absence of deletion/mutation methods.
The cumulative Phase 4FN–4FR regression passed 48 tests in 118.76 seconds. Ruff and mypy passed
for the phase-owned files.

## Rejected alternatives

- Rewriting the first retained node as a new genesis was rejected because it destroys its original
  chain identity.
- Deleting by age alone was rejected because it cannot guarantee a bounded removal count.
- Applying the plan in this module was rejected; deletion authority is explicitly out of scope.

## Removal and next dependency

Remove the module, test, and report. No production cleanup or rollback is required. Phase 4FS
should simulate interrupted publication and retention using temporary directories only.
