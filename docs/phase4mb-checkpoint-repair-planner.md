# Phase 4MB — Checkpoint Corruption Localization and Minimal Repair Planning

## Outcome

Phase 4MB compares Phase 4MA checkpoints with a separate hash-bound expected-evidence manifest,
locates the first corruption boundary, and preserves only the unambiguous trustworthy prefix. This
detects altered evidence even when a checkpoint hash has been recomputed.

## Repair boundary

Plans contain declarative `DISCARD_SUFFIX`, `REACQUIRE_EVIDENCE`, `REPLAY_FROM_CHECKPOINT`,
`REBUILD_RESUME_TOKEN`, `NO_REPAIR`, or `ABORT_RECOVERY` actions. Replay is recommended only from a
fully verified checkpoint carrying the exact fail-closed invariant snapshot. No source edit, invented
hash, or skipped revalidation is authorized.

## Safety and removal

The planner does not edit checkpoints, persist plans, execute repairs, activate policy, modify
runtime, restart WSL or services, access a network, or create orders. Remove the script, focused
test, and report to roll back.

## Reproducible evidence

- Rehashed evidence corruption at checkpoint index 4 localizes to boundary 4 with plan SHA-256
  `cd3d0418dc8844324934adc5247ecea5a349901e72f239105cd6f086ec942078`.
- A six-checkpoint truncation localizes to boundary 6 with plan SHA-256
  `cd27134a1ab7c5898eb3093ed4c57db0b87d5635341c0a292764b1fa2b310660`.
- The focused suite covers all ten state boundaries plus hash, link, invariant, ordering, time,
  replay, terminal-suffix, manifest, and non-capability cases.

## Next phase

Phase 4MC — Minimal-repair plan mutation audit and non-escalation proof.
