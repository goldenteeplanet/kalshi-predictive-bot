# Phase 4OW — Checkpoint Corruption and Redundant-Copy Recovery

## Outcome

Phase 4OW wraps three checkpoint-chain copies in independent replica hashes and requires two valid
replicas to agree on one complete chain. Seven deterministic corruptions cover bit flips,
truncation, reordering, duplication, stale prefixes, cross-run substitution, and envelope hash
drift. Each single-copy corruption is detected, repaired in memory from the matching quorum, and
resumes to the unchanged uninterrupted orchestration hash.

Simultaneous corruption below quorum, identity replay, unknown replicas, or multiple quorum-valid
chains fail closed with no canonical chain. Settlement binding is preserved and recovery artifacts
remain non-executable.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OV/4OW regression: `12 passed in 34.69s`.
- Corruption classes: `7`; single-copy repairs: `7`; survivors: `0`.
- Unique converged orchestration SHA-256:
  `199cd825cb673fcbd1ea5b2136627a08fb998b10934c30a28491ca178ae79ed7`.
- Corruption-matrix verdict: `PASS`; SHA-256:
  `b7b81d72d3c26a87805fd4b62890af8db3354478f9986d20eaf03c52ef29e3d3`.

## Safety and removal

Corruption and repair are offline, in-memory, and non-persistent. They cannot create paper orders or
enable demo, live, or autopilot execution. Remove the three Phase 4OW files to roll back.

## Next phase

Phase 4OX — Checkpoint replica placement and correlated interruption recovery proof.
