# Phase 4OJ — Multi-Anchor Quorum and Disaster-Recovery Provenance

## Outcome

Phase 4OJ replaces a lone rollback anchor with independently identified, hash-bound anchor
attestations. Certification requires one unique quorum statement covering generation, journal head,
frozen/recovered state, and the source anchor hash. Authority replay, unknown identities, stale
generations, mixed histories, tampering, and equivocation fail closed.

The disaster-recovery provenance artifact binds the quorum certificate, source anchor, dual-copy
reconciliation, and canonical copy. A binding mismatch or failed copy reconciliation returns the
frozen state with capabilities disabled. Input ordering cannot affect certificates or provenance.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OI/4OJ regression: `11 passed in 31.56s`.
- Two-of-three anchor certificate: `PASS`; SHA-256:
  `771dd0ff9656b0cd8374d902e8b227bd091fdc62c5d249083e8e3f6c62fe7830`.
- Bound disaster-recovery provenance: `PASS`, state `FROZEN`, capabilities `false`.
- Provenance SHA-256: `9013d5951e6b1579fb054d7eaf09fcb20f2473f1612f55d50f6e62baf1f673e3`.

## Safety and removal

All certification and provenance work is offline, in-memory, and non-persistent. It cannot create
paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4OJ files to roll
back.

## Next phase

Phase 4OK — Disaster-recovery provenance mutation campaign and independent verifier.
