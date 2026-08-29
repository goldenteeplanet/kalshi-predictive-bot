# Phase 4NT — Recovery Quorum and Split-Brain Proof

## Outcome

Phase 4NT adds independent witness identities with versioned keys. Attestations bind signer,
checkpoint identity and epoch, workload, cumulative hash, safety state, issuance epoch, and a
deterministic offline signature. Recovery requires a configurable signer threshold and selects the
newest quorum-certified checkpoint within staleness and rollback bounds.

The verifier rejects duplicate votes, unknown or revoked witnesses, revoked key versions, mixed
workloads, forged signatures, unsafe attestations, checkpoint mismatch, equivocation, insufficient
or stale quorum, and split-brain quorum. The matrix covers witness loss, delay, key rotation, quorum
restoration, forgery, and duplicate voting.

## Verification evidence

- Focused Phase 4NT suite: `7 passed`
- Quorum-matrix cases: `7`
- Certified recovery cases: `2`
- Fail-closed cases: `5`
- Quorum-matrix verdict: `PASS`
- Matrix SHA-256: `66a38dcb8b82b1caa4f719d6aff6f53a7924e147f7ad26d50594957214e5838d`

## Safety and removal

Witnessing and recovery are offline and non-persistent, with no paper, demo, live, autopilot, order,
network, service, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NU — Byzantine witness tolerance and quorum-policy sensitivity proof.
