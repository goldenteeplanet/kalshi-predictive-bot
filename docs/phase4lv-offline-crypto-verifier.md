# Phase 4LV — Offline Verifier Interface and Trust-Root Pinning

## Outcome

Phase 4LV defines a strict offline verifier interface and pins root, intermediate, subject, issuer,
audience, certificate policy, public material, and algorithm digests. It binds canonical Phase 4LU
request bytes to a deterministic public-binding signature fixture and rejects replay, substitution,
malleable encodings, weak algorithms, and expired certificate metadata.

## Production boundary

The available WSL runtime has no approved public-key verification backend. This phase therefore uses
an explicitly non-production fixture algorithm to test the interface and trust policy. Every result
returns production readiness `REFUSE`; fixture success must never be interpreted as production
authenticity. No OS certificate store or remote trust source is consulted.

Merkle inclusion remains independently checked by the Phase 4LU contract. Phase 4LV does not weaken
or replace that gate; a production composition gate must require both results before readiness can
ever pass.

## Reproducible fixture evidence

The fixed public fixture passes with trust-policy SHA-256
`6e786068e179a8e659017fe4801a0704a0c75cb0c418718cddd7a90f91c14aad`, verification identity
SHA-256 `3de5d12c62965c1c6a8911266ecc2b00f4942d5d104cc6434cb03915ef930140`, and verification
SHA-256 `32b4a8da33e726de7d85019122b049addc8624e7ac7a811e310b56409e33d850`. Production
readiness remains `REFUSE`.

## Safety and removal

Only public hashes and deterministic fixture signatures are accepted. The implementation cannot
generate, import, store, or access private keys, use a network, control services, or create orders.
Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LW — Production-verifier dependency readiness and supply-chain attestation gate.
