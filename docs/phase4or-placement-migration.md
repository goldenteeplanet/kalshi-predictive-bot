# Phase 4OR — Placement Migration and Diversity-Drift Detection

## Outcome

Phase 4OR adds an independently approved add-before-remove placement ceremony. It binds the source
set and destination hashes and reruns complete diversity and correlated-loss audits at the source,
expanded, and final stages. No removal is certified unless every intermediate state preserves
quorum survivability.

Continuous drift detection compares the observed placement hashes with the certified final set and
reruns the diversity audit. Host, filesystem, administrator, failure-domain, hidden-dependency,
history, membership, or ceremony drift immediately revokes the recovery claim and returns a frozen
state.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OQ/4OR regression: `13 passed in 29.22s`.
- Migration ceremony: `PASS`; SHA-256:
  `97dbd2e53cd0b29d7422058d35f13369e28ee0c91b53633d3aec76743441c352`.
- Minimum survivors by stage: source `3`, add destination `4`, remove source `3`.
- Continuous drift report: `PASS`, `READY`, recovery claim `true`.
- Drift-report SHA-256:
  `733fb2250fa4b07dde2d519e9e7214fa94380f065e5eb58f28613e6230eb802c`.

## Safety and removal

Migration is modeled offline and produces no infrastructure or runtime writes. It cannot create
paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4OR files to roll
back.

## Next phase

Phase 4OS — Pre-settlement adversarial engineering program aggregate certification.
