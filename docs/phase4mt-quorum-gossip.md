# Phase 4MT — Quorum Witnesses and Gossip Consistency

## Outcome

Phase 4MT binds independent witness statements to one trust-store generation, head record, store
identity, implementation identity, and observation time. A quorum requires fresh statements from
distinct registered independence groups; duplicates, collusion, revocation, staleness, conflicting
heads, identity substitution, and equivocation refuse.

## Gossip boundary

The deterministic gossip audit permits delayed delivery and temporary partitions when every node
eventually converges on one head without losing prior knowledge. Conflicting heads remain visible
and refuse even after all nodes learn both views. Neither quorum nor gossip authorizes automatic
reconciliation.

## Reproducible evidence

- complete Phase 4MP–4MT suite: 53 passed
- independent quorum size: 2
- quorum SHA-256: `1114abd80eee45776717c2095d853e35286ba208cde7e07ab6aa9fb6a566c1ea`
- delayed-gossip convergence SHA-256: `ddaa88c1297db80a2167ba968f3f7c9eca2be424ba3396dd524a14b4d25b1a15`
- conflicting-view refusal SHA-256: `49a9c2de62e9cc4ed761c7c14c396e5f47b000b4ffa17f019509b494924b7f86`

## Safety and removal

The proof is offline and in-memory. It provides no network, persistence, runtime, service, repair,
acceptance, or order capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MU — Witness key lifecycle, threshold-policy versioning, and compromise-recovery proof.
