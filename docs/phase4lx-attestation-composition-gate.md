# Phase 4LX — Independent Attestation Authenticity Composition Gate

## Outcome

Phase 4LX composes hash-valid Phase 4LW dependency readiness, Phase 4LU transparency validation,
Phase 4LV verifier results, and exactly two role-separated attestations. Both attestations bind every
upstream artifact hash and the component subject, and require distinct pinned builder and scanner
authorities, freshness, non-self-assertion, and disjoint replay identities.

## Production boundary

The reproducible fixtures may pass structural composition but always remain production `REFUSE`.
Self-declaring `INDEPENDENT_PRODUCTION` cannot override Phase 4LW or Phase 4LV production refusal or
Phase 4LU's lack of cryptographic signature verification. No genuine production attestations are
fabricated by this phase.

## Reproducible composition evidence

The ordered fixture pair returns fixture `PASS` and production `REFUSE` with composition SHA-256
`9d027ce4f849badbf8bd56be5ba4d8e910f3a1d70d23cc16c8ab8cd44b4a3d00`. Re-labeling both fixtures
as production evidence still returns production `REFUSE`, with SHA-256
`d5f839f6fe8bca4f0e7cf2ad86275471f16771bed7a6deba5ed0a1697661873d`. Production authority pins
are intentionally unavailable.

## Safety and removal

The gate validates supplied values only and cannot generate attestations, install packages, access
keys, networks or databases, control services or WSL, deliver notifications, or create orders.
Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LY — Attestation quorum, revocation, and trust-policy rotation contract.
