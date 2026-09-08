# Phase 4MR — Review-History Compaction and Audit Anchors

## Outcome

Phase 4MR deterministically replaces a validated workflow prefix with a content-bound audit anchor
while retaining a configurable tail. The anchor preserves the complete-history digest, exact prefix
head, event counts, final state, review epoch, closure-certificate digest, packet identity, and
implementation identity.

## Verification boundary

Compact verification requires the independently trusted anchor SHA-256 and checks anchor integrity,
tail integrity, sequence continuity, chain linkage, counts, and the final head. Exact restoration
requires the archived prefix, verifies its digest, reconstructs the original history, and reruns the
complete Phase 4MP workflow validator. The anchor is a verification boundary, not a substitute for
independent trusted-anchor storage.

## Reproducible evidence

- focused Phase 4MP–4MR suite: 36 passed
- full-history SHA-256: `7b516ffc42d4efe144d39b428aa47a337cf385c238849afce4fa70d9b02371d1`
- audit-anchor SHA-256: `a2b511d21ec46573cdbb7b146690dc244e5abc168db5330ba719d1b7b9ea07af`
- compaction SHA-256: `c1b691e98011290bb800f96a77cca7283c0f65591d607eb23e78971ed988b0b1`
- exact-restoration SHA-256: `f267af4b0e98b1a80a3fd97c9315c082d89d55f2bcdd97a4d8d185d47bdbb667`
- restored events: 10

## Safety and removal

All operations use copied in-memory values. There is no persistence, network access, runtime write,
service control, or order capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MS — Audit-anchor rotation, trust-store rollover, and split-view detection proof.
