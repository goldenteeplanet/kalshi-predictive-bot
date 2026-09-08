# Phase 4NU — Byzantine Quorum-Policy Sensitivity

## Outcome

Phase 4NU distinguishes crash availability from Byzantine safety using explicit quorum-intersection
math. For N witnesses and threshold Q, the safety bound is `2Q - N - 1`, the liveness bound is
`N - Q`, and simultaneous tolerance is their minimum. Claimed tolerance beyond that guarantee
refuses policy certification.

Bounded deterministic enumeration classifies honest, offline, delayed, equivocating, colluding,
forged, compromised, revoked, correlated, rotating, and restored witness conditions as preserving
safety, liveness, both, or neither. Policy comparison provides a deterministic recommendation and
residual-risk statement; emergency threshold changes that weaken tolerance roll back.

## Verification evidence

- Focused Phase 4NU suite: `8 passed`
- Bounded failure cases enumerated: `27`
- Named witness scenarios: `12`
- Deterministic recommendation: `3-of-4`, claimed Byzantine tolerance `1`
- Enumeration SHA-256: `c24319f5ecc7da673a743290c4f3f971081888852e52baace248450ef9d8f02a`
- Scenario-matrix SHA-256: `98c0894f428595149f1d3ad4a2fcf2707bc7aa380c32f47b40a8eee2f52f3869`
- Policy-comparison SHA-256: `24eb05e9e8c4f73fd8634942a609549d45a509edbc28fe5a06d65391ac382b3a`

## Safety and removal

The model is offline and non-persistent and cannot access runtime services, create orders, or enable
paper, demo, live, or autopilot execution. Remove the three phase files to roll back.

## Next phase

Phase 4NV — Witness diversity, correlated-domain placement, and common-mode failure proof.
