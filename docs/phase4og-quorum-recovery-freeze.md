# Phase 4OG — Partial Quorum Recovery and Emergency Freeze

## Outcome

Phase 4OG adds a hash-bound emergency freeze for quorum loss or suspected compromise. A valid
freeze binds the current epoch and explicitly disables trusted-time certification and freshness
renewal. The state remains frozen unless a recovery artifact reaches a stricter threshold of
independent recovery authorities.

Recovery creates a child membership epoch, preserves cumulative revocations, and cannot reintroduce
removed witnesses. Invalid or wrong-epoch freezes, ordinary or non-independent authority, unilateral
unfreezing, weak recovery policy, forged artifacts, replayed attempts, and competing recovery
children all fail closed and retain the frozen state.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OF/4OG regression: `11 passed in 26.74s`.
- Freeze SHA-256: `5622c085952700ee75cc65e2f6f06078e01985c6d3f147d6bed15e1557c5bb4d`.
- Frozen capability gate: `REFUSE`; SHA-256:
  `4e08093a2dcfa6b6a904738dc562769d99d937c0c53a5d0a8b092fb0be5a614d`.
- Independent recovery: `PASS`; SHA-256:
  `9de2a6a7a602433426c535d90911ce61980657fb83eab0c1b3bda155f5e0202b`.
- Recovered child epoch: `60c80884e83d172435730713a0c003727b7ebf7b2ff25c06cff989ea0c30bfa4`.
- Recovery adjudication: `PASS`; SHA-256:
  `53a27d89923c701edcb7e4e1afc51d5b282fbc185b79a3cdde290558390b26f9`.

## Safety and removal

The recovery model is offline, in-memory, and non-persistent. It cannot mutate infrastructure or
runtime state and cannot create paper orders or enable demo, live, or autopilot execution. Remove
the three Phase 4OG files to roll back.

## Next phase

Phase 4OH — Emergency-freeze durability, restart replay, and state-loss recovery proof.
