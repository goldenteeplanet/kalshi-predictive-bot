# Phase 4NK — Cross-Platform Canonicalization and Replay Portability

## Outcome

Phase 4NK defines platform-neutral canonical JSON for UTC timestamps, decimals, Unicode NFC, object
keys, UTF-8, and line endings. A portability proof canonicalizes two platform-shaped inputs, creates
their Phase 4NJ bundles, independently replays both, and requires identical canonical bytes, bundle
hashes, output hashes, and verdicts.

The boundary refuses timezone ambiguity, non-finite numbers, locale-dependent numerics, duplicate
Unicode-normalized keys, unsupported encodings, absolute host paths, platform metadata, unsupported
types, and canonicalization-version drift.

## Verification evidence

- Focused Phase 4NJ–4NK suite: `14 passed`
- Canonical input SHA-256: `2cd85111daf9f45900ec76379651025fb0cfee2821b14bbc96218b683aee1212`
- Portable bundle SHA-256: `bb48a73a63a021051ea16ad9049c3200e2223843abefa32e05d01eaa89366687`
- Replay output SHA-256: `379c3690adb5c44edc393a655b9e29d2cef16fbe9696c6421e5e94d71392b86f`
- Portability verdict: `PASS`
- Proof SHA-256: `321b75ba81bbb0e19ae78a90c48dcec305bc031df237738ddaf43aae2cf069bd`

## Safety and removal

The proof is offline, non-persistent, and unable to access networks or runtimes or create paper,
demo, live, or autopilot orders. Remove the three phase files to roll back.

## Next phase

Phase 4NL — Golden corpus, compatibility vectors, and verifier conformance proof.
