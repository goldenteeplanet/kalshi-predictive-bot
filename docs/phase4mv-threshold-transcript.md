# Phase 4MV — Threshold Transcript and Signer-Subset Audit

## Outcome

Phase 4MV simulates a threshold-signing transcript bound to the pinned policy, trust-store head,
message, signing round, nonce commitments, key identities, and exact signer subset. Independent
recomputation verifies every contribution and produces a deterministic aggregate evidence hash.

## Security boundary

This is explicitly not production cryptography. It detects duplicate signers, nonce reuse, missing
contributions, subset or transcript substitution, mixed policies, unusable keys, replay, and partial
crashes. Prepared contributions are discarded; durable contributions are preserved exactly.

## Reproducible evidence

- complete Phase 4MP–4MV suite: 69 passed
- transcript-context SHA-256: `a9f36c83abbd2a9f310a2f7898924efbe8149c932324246cc9821e7dead5b260`
- simulated aggregate SHA-256: `f9fbc21a5abd987c0ba6099c4a47cb869b9bd9b1d8563548df075d91dba3ee9c`
- independent verification SHA-256: `e9cbb9c89f5ab4cd16e7893329325eae03df634d517e432e75b59fd9843161bb`
- crash recovery SHA-256: `aa13d2068bb1b24d76e1c5504ac75778724595b3171bfaa3b6476ff6234c25fd`
- cross-transcript nonce-reuse audit SHA-256: `b72e724efcc9be33e5d3f0219b57c90100e2031040e9861560064bcb6e96cd79`

## Safety and removal

The simulator has no signing key material, cryptographic-signature claim, persistence, network,
runtime, service, repair, acceptance, or order capability. Remove the three phase files to roll back.

## Next phase

Phase 4MW — Transcript archive retention, deletion-tombstone, and legal-hold simulation.
