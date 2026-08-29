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

- Canonical package size: 47,822 bytes across seven components.
- Package SHA-256: `47f8c2f1d1b7922a5b46602d119dc03f367a1378843a181fba132bb5a375ffab`
- Manifest SHA-256: `556e837738c8014f11f134ead7f179db9f91f46a08b177443ab889277e4c0a1a`
- Offline verification SHA-256:
  `523c4ecd48e76c44146559c863e9baf341c46e99d11f7827aebc48706021c74e`
- Real Phase 4MJ certification and passing Phase 4MK audit outputs were used; verification returned
  `PASS` with no errors.

## Safety and removal

The package is built and verified in memory. No secret may be included. The implementation does not
write files, persist production data, execute repairs, change runtime state, control WSL or services,
access the network, or create any order. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MM — Handoff package parser fuzzing and resource-bound certification.
