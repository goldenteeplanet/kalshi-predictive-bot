# Phase 4OV — Renewal Interruption and Exactly-Once Resume

## Outcome

Phase 4OV creates a checkpoint for the empty prefix and after each of six offline renewals. Every
checkpoint binds the original record set, run timestamps, uninterrupted orchestration hash,
completed evidence IDs and step hashes, previous checkpoint, and unchanged settlement record.

Resume regenerates the canonical orchestration and accepts only its exact checkpoint prefix. Every
interruption point converges to the same final orchestration hash. Forged, truncated, reordered,
duplicated, stale, cross-run, skipped-step, or settlement-mismatched checkpoints fail closed without
repeating a completed renewal.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OU/4OV regression: `12 passed in 30.91s`.
- Checkpoints/interruption points: `7`; unique converged final hashes: `1`.
- Checkpoint-chain head: `42258aa8e8507b8a87677331b7b95b0bc8d311b5f0ae8eca9732c3a0b3cafa9e`.
- Uninterrupted orchestration SHA-256:
  `199cd825cb673fcbd1ea5b2136627a08fb998b10934c30a28491ca178ae79ed7`.
- Full-prefix resume: `PASS`; SHA-256:
  `e553daa012d3606a95ddf425c7beaf158bb2a77f6d5b3fa31425a5a44e01c988`.
- Settlement unchanged: `true`; executable: `false`.

## Safety and removal

Checkpoint and resume proofs are offline, in-memory, and non-persistent. They cannot create paper
orders or enable demo, live, or autopilot execution. Remove the three Phase 4OV files to roll back.

## Next phase

Phase 4OW — Renewal checkpoint corruption matrix and redundant-copy recovery proof.
