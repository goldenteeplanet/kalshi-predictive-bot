# Phase 4LY — Attestation Quorum, Revocation, and Trust-Policy Rotation

## Outcome

Phase 4LY validates monotonic, predecessor-bound fixture trust-policy rotations with fixed builder,
scanner, and recovery roles and a two-role quorum. Authority replacement requires an exact bound
revocation signed by the two non-target current authorities; compromise, expiry, and administrative
rotation are the only reason codes.

## Rotation invariants

Epoch reuse, rollback or skipping, quorum reduction, duplicate authority identities or digests,
self-approval, replay, stale evidence, missing revocations, unsafe overlap, fixture promotion, and any
runtime, key, network, service, or trading authority broadening fail closed. Production readiness is
always `REFUSE` because no genuine production authority set exists.

## Reproducible transition evidence

The routine epoch-one-to-two fixture rotation returns fixture `PASS`, production `REFUSE`, and
rotation SHA-256 `6fd78f3b4f66514d6c6be47e7266fc5616b603affedc8a91002e9473044a6aab`. The compromise recovery
fixture that replaces the builder using scanner and recovery approvals and revocation signatures
returns `PASS` with SHA-256 `b19b1344028b2973f50d4e36bd863bb30f88032b5e8c7959b54e7430202950b9`.

## Safety and removal

The contract validates values only. It cannot activate policy, publish revocations, access keys or a
network, control services, or create orders. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LZ — Trust-policy disaster recovery and offline break-glass simulation.
