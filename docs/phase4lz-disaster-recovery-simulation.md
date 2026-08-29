# Phase 4LZ — Trust-Policy Disaster Recovery and Break-Glass Simulation

## Outcome

Phase 4LZ simulates loss or compromise of up to two fixture authorities, policy corruption, replay
cache loss, clock uncertainty, and recovery-media failure. It requires a hash-precommitted plan,
two-person custody, bounded emergency time, monotonic recovery epochs, complete revocation, mandatory
rotation, replay reconstruction, full three-role quorum restoration, and independent after-action
evidence.

## Fail-closed boundary

Reused incidents, single-person custody, unavailable or unpinned media, ambiguous time, incomplete
revocation or quorum, missing replay reconstruction, absent after-action review, authority broadening,
and production promotion all refuse. Recovery material is never accessed; only its digest and
availability are modeled.

## Reproducible scenario evidence

The single-builder compromise scenario returns fixture `PASS`, production `REFUSE`, and simulation
SHA-256 `ce061ab6403187d5a9b7413ab53bd01112e25ace767e4b6177d7996eaa80d3e3`. The dual builder/scanner
loss with replay-cache reconstruction returns `PASS` with SHA-256
`09c43466ed7df3328583e88e665513bd4314efe874a6e8ea926817b0b7775aef`.

## Safety and removal

The implementation only validates supplied scenario values. It cannot activate policy, access
material, modify runtime state, restart WSL or services, use a network, or create orders. Remove the
script, focused test, and report to roll back.

## Next phase

Phase 4MA — Disaster-recovery state-machine and interruption-resume invariants.
