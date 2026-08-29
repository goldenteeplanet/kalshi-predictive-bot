# Phase 4ML — Recovery Certification Package and Offline Handoff Contract

## Outcome

Phase 4ML packages Phase 4MJ certification, Phase 4MK reproducibility, focused-test evidence,
75-file inventory, schemas, residual risks, and verification instructions into deterministic
canonical JSON. Ordered descriptors bind every component path, media type, byte size, and hash.

## Offline handoff boundary

The manifest binds the certified range and descendant heads, intended audience, seven-day maximum
lifetime, and an all-false deferred-capability map. Verification checks canonical bytes, component
order and uniqueness, size bounds, hashes, semantic PASS state, secrets, audience, and time. It
emits only a relative safe-extraction plan and performs no extraction.

## Reproducible evidence

- Canonical package size: 47,744 bytes across seven components.
- Package SHA-256: `7ce7e45498b6a6f7fec2994d2e625a12c73f27ce09144d687a6ae781dbb67302`
- Manifest SHA-256: `d1e17fdf508e072961a5a0d5d19de667c0ac3f62cb627064333d423873ca2834`
- Offline verification SHA-256:
  `3055058cf9cf257e9bc2773f17f36c42d338aec8c6cb97069e4ec7dbaf4c81dc`
- Real Phase 4MJ certification and passing Phase 4MK audit outputs were used; verification returned
  `PASS` with no errors.

The Phase 4MJ component excludes checkout path, descendant repository head, and the corresponding
outer certificate hash while retaining and hash-binding all certified semantics. Two runs from
different descendant repository heads therefore produce identical package bytes.

## Safety and removal

The package is built and verified in memory. No secret may be included. The implementation does not
write files, persist production data, execute repairs, change runtime state, control WSL or services,
access the network, or create any order. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MM — Handoff package parser fuzzing and resource-bound certification.
