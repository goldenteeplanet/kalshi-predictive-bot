# Phase 4OF — Quorum Membership Rotation and Compromise Containment

## Outcome

Phase 4OF adds hash-linked membership epochs and dual-quorum rotation artifacts. A rotation needs
the configured quorum from both the current and proposed memberships, with at least one continuing
witness approving on both sides. Epoch numbers must increase exactly once and every epoch binds its
parent hash, membership, threshold, cumulative revocations, settlement blocker, and safety state.

Revocation is permanent across the verified chain: a removed or compromised witness cannot return
as an active member. Missing approvals or overlap, unknown revocation targets, invalid thresholds,
epoch replay or rollback, forged ancestry, lost revocation history, altered approvals, and competing
children for one parent all fail closed.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OE/4OF regression: `12 passed in 27.64s`.
- Dual-quorum rotation and chain verdicts: `PASS`.
- Epoch 1 SHA-256: `c0aab7bf87b71f6d4ca875655da582f4b8ecf7db3004a9ec3493c2f077291b91`.
- Rotation SHA-256: `3518b23aeb5af7211633a8f0681bd30cff3a4bba34d09e9f4a6c68bfebd46442`.
- Epoch 2 SHA-256: `6a4f53cf02c0c999cf67e1ab91cafe6c685ae3727bf2aeb29938d2a3ef84cfc3`.
- Chain verification SHA-256:
  `8a6556a056a6dcd7f75dc14168a28c6a369dde81ed869e30758b580a09ae2c52`.
- Revoked-witness reintroduction: `REFUSE` with `REVOKED_WITNESS_REINTRODUCED`.

## Safety and removal

The rotation model is offline, in-memory, and non-persistent. It cannot mutate infrastructure or
runtime state and cannot create paper orders or enable demo, live, or autopilot execution. Remove
the three Phase 4OF files to roll back.

## Next phase

Phase 4OG — Membership-rotation recovery under partial quorum loss and emergency freeze.
