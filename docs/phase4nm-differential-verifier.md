# Phase 4NM — Differential Verifier Cross-Check

## Outcome

Phase 4NM adds a structurally independent manifest parser, canonicalizer, hash validator, invariant
checker, and scenario replay orchestrator. It does not call the primary bundle verifier,
canonicalizer, or corpus runner. The differential gate executes the secondary implementation across
all Phase 4NL vectors and requires identical verdicts and normalized error classes.

Targeted asymmetric overrides demonstrate detection of parser, canonical-byte, manifest/hash,
replay, and safety-policy disagreement. Missing corpus cases and changed primary expectations also
refuse the proof.

## Verification evidence

- Focused Phase 4NL–4NM suite: `15 passed`
- Golden vectors cross-checked: `23`
- Differential disagreements: `0`
- Secondary verifier source SHA-256: `9452f791a6599f29da3604ed2ad75b4137d5754dbb1c70460bc51b52c2f86175`
- Differential verdict: `PASS`
- Proof SHA-256: `1aed234ef4475a6dcd6d102b0091257f05f5c433e4ac645043093b557697ac4f`

## Safety and removal

The secondary implementation is offline and non-persistent and has no paper, demo, live,
autopilot, order, network, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NN — Metamorphic replay properties and transformation-invariant proof.
