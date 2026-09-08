# Phase 4MS — Audit-Anchor Rotation and Split-View Detection

## Outcome

Phase 4MS adds deterministic, hash-linked audit-anchor rotation with bounded predecessor overlap,
explicit revocation, externally pinned trust-store identity, and fail-closed restoration. It detects
same-predecessor equivocation while allowing ordinary prefix propagation between observers.

## Trust boundary

Hash linkage proves internal continuity; authority comes from an independently pinned trust-store
SHA-256. Unknown, revoked, expired, rolled-back, tampered, missing-predecessor, and split-view
anchors refuse. Detection never automatically reconciles divergent histories.

## Reproducible evidence

- complete Phase 4MP–4MS suite: 44 passed
- trust generations: 3
- trust-store SHA-256: `93012c3de1ece7446ad2cf0b8b10e9b4b686ba58701c05de36b83a06e5633a1b`
- head-record SHA-256: `4345d705582b83e886edc3ea60921acc03c3306f2efec1575d534e09de35b135`
- trust-validation SHA-256: `c65bf9cc57f59697d3ce443caaf66f96fb72dc67a36c2620afa2b715a4c54ef5`
- injected split-view audit SHA-256: `d8837c234de09bfc83eed638fb4584eb65bd98dcb7ded5e674704610927c01be`

## Safety and removal

All operations are in-memory simulations with no persistence, network access, runtime writes,
service control, or order capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MT — Trust-store quorum witnesses and gossip consistency proof.
