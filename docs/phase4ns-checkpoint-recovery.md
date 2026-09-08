# Phase 4NS — Fault-Injected Checkpoint Recovery

## Outcome

Phase 4NS validates checkpoint shape, signature, workload binding, position, and independently
recomputed cumulative prefix before recovery. Deterministic precedence selects the newest valid
checkpoint regardless of candidate ordering or exact duplicates, subject to a rollback bound.

Thirteen matrix cases cover truncation, bit flips, stale and future positions, wrong workloads,
missing fields, duplicates, reordered chains, corrupted cumulative hashes, interrupted creation,
partial writes, rollback to the last valid checkpoint, and same-epoch disagreement. Successful
recovery must reproduce uninterrupted suffixes, final hash, coverage, refusal classes, and
provenance.

## Verification evidence

- Focused Phase 4NS suite: `7 passed`
- Fault matrix cases: `13`
- Recoverable cases: `4`
- Fail-closed cases: `9`
- Uninterrupted final SHA-256: `ce833688202093cf89a35fd75937064790df0b277eb0baef261344445505658c`
- Recovery-matrix verdict: `PASS`
- Matrix SHA-256: `f1239e9561da78b96a78c0455df4c778592d98cb944d71b46101a956d3853c76`

## Safety and removal

Recovery is in-memory, offline, and non-persistent, with no paper, demo, live, autopilot, order,
network, service, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NT — Recovery quorum, independent checkpoint witnesses, and split-brain proof.
