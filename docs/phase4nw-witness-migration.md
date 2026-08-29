# Phase 4NW — Witness Placement Migration and Cutover

## Outcome

Phase 4NW generates an add-before-remove migration from a correlated legacy witness set to a
diverse 3-of-4 Byzantine-tolerant layout. Replacement witnesses pass unique-key enrollment,
checkpoint catch-up, warm-up, dual attestation, and certification before voting. A temporary 5-of-7
policy supports staged legacy removal, with rollback points before the atomic final cutover.

Every stage checks policy mathematics, certified-voter status, effective independence, rollback
availability, and final Phase 4NV placement evidence. The simulator rejects premature thresholds,
remove-before-add, incomplete catch-up, key reuse, uncertified activation, shared-domain regression,
unsafe revocation, split brain, missing rollback quorum, and unverifiable final placement.

## Verification evidence

- Focused Phase 4NW suite: `7 passed`
- Deterministic migration stages: `32`
- Certified rollback points: `2`
- Final policy: `3-of-4`
- Final effective independence: `4`
- Plan SHA-256: `aeb77cf20b320d297065341b98c0452f9c9db3ad6ca67cbdf1be4d9cfa623acb`
- Migration verdict: `PASS`
- Simulation SHA-256: `77db5fc3b2038fc97ebacca6b434c1299a33f7369672dc34d436b0682676b1c7`

## Safety and removal

This is an offline, non-persistent migration simulation. It cannot alter infrastructure or runtime
services, create orders, or enable paper, demo, live, or autopilot execution. Remove the three phase
files to roll back.

## Next phase

Phase 4NX — Migration authorization transcript and two-person cutover approval proof.
