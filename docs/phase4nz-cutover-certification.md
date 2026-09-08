# Phase 4NZ — Cutover Evidence and Independent Certification

## Outcome

Phase 4NZ packages the migration plan, scoped authorization and approvals, ceremony input and
transcript, checkpoint/quorum/rollback anchors, placement evidence, operators, stage outcomes,
thirteen fault-injection outcomes, and safety state into a versioned per-artifact manifest and
envelope hash.

An independent verifier reconstructs the plan and request identities, approval signatures and
scope, ceremony order and event hashes, role separation, transcript hash chain and root, stage
outcomes, anchors, terminal state, and complete fault coverage without calling the primary ceremony
simulator. Missing, altered, reordered, unsafe, nonterminal, ambiguous, or schema-drifted evidence
refuses certification.

## Verification evidence

- Focused Phase 4NZ suite: `7 passed`
- Independently hashed artifacts: `13`
- Manifest entries: `13`
- Required fault outcomes reconstructed: `13`
- Evidence-bundle SHA-256: `407f0fcb2fe4746899a773bcf839fbb4c0487330de5e6b69ac7e23f4a81f06e0`
- Certification verdict: `PASS`
- Certification SHA-256: `b0804bf68251f89e77294babb3bb57ceb1a2103d6337c2a48b7572492f0c5bfd`

## Safety and removal

The evidence bundle and audit are offline and non-persistent and cannot alter infrastructure or
runtime services, create orders, or enable paper, demo, live, or autopilot execution. Remove the
three phase files to roll back.

## Next phase

Phase 4OA — End-to-end adversarial validation release candidate and aggregate gate.
