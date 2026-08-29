# Phase 4MG — Snapshot Restoration and Cross-Version Migration Simulation

## Outcome

Phase 4MG provides one explicit migration registry edge from a validated Phase 4MF v1 snapshot to a
hash-bound v2 restoration artifact. Migration is deterministic and idempotent at v2; unknown,
skipped, and downgrade paths are refused.

## Restoration boundary

Restoration begins from the exact snapshot generation and head anchor, preserves the complete
burned-nonce set and transaction states, and accepts only a contiguous hash-linked retained suffix.
State widening, nonce-owner duplication, generation gaps, incompatible anchors, token or receipt
substitution, field loss, and divergent migration results fail closed.

Restoration independently pins the source snapshot hash, source generation, source head hash, and
prior-snapshot anchor; none may be inferred solely from the migrated artifact.

## Reproducible evidence

- Migrated artifact SHA-256:
  `2743494ce0478ca1bb3007e6771526702a969f53e7f8a9f623fb6ab25081e3f5`
- Empty-suffix round-trip restoration SHA-256:
  `073ee229c886ef55d63166578dd422d49ed64606f34a9d79fa7170bd3b6a8c75`
- Retained-suffix restoration SHA-256:
  `96136f1f83b0be6931eabe369540ca4230cd4e13da1b4ce42f2da7fd6edfdc3b`
- Idempotent-lineage audit SHA-256:
  `f2dfcc17e36b01b2a9f4743a2a1339a1036cebe2d412b65ac46de98ca607554f`

## Safety and removal

Migration and restoration are in-memory simulations. They do not write production data, persist
snapshots, execute repairs, change runtime state, control WSL or services, access the network, or
create any order. Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4MH — Restoration differential replay and state-equivalence certification.
