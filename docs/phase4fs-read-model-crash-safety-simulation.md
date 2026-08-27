# Phase 4FS — Read-Model Crash-Safety Simulation

## Outcome

Phase 4FS adds a real-filesystem atomic-publication simulator restricted to explicitly marked
`phase4fs-*` fixture directories. It exercises interruption before a temporary write, after a
partial write, after a complete fsync, and after atomic replacement.

## Contract and recovery rules

- Workspace must have a `phase4fs-` name and exact `.phase4fs-simulation` marker.
- Simulated artifacts use `phase4fs-read-model-crash-simulation-v1` and canonical SHA-256.
- Artifact bytes are bounded before any write.
- Before replacement, an existing hash-valid `current.json` remains authoritative.
- A partial candidate is reported invalid; a complete fsynced candidate is reported valid but is
  not promoted by recovery inspection.
- After `os.replace`, only the new complete current artifact is authoritative.
- A malformed current artifact fails closed even when a valid candidate exists.
- Inspection does not delete, repair, promote, or rewrite evidence.

## Safety and verification

All file mutation occurs under pytest temporary directories created specifically for this phase.
The workspace name and marker checks reject ordinary report/database paths before creation or use.
Tests cover successful progress, every crash point, empty state, missing marker, exact byte bounds,
overflow, hash tampering, partial current data, non-simulation paths, and sibling-file isolation.
No production database, runtime artifact, service, forecast, decision, order, fill, or settlement is
opened or changed. The cumulative Phase 4FN–4FS regression passed 57 tests in 68.78 seconds. Ruff
and mypy passed for the phase-owned files.

## Rejected alternatives

- An in-memory-only state machine was rejected because it would not test fsync and replace behavior.
- Automatic candidate promotion was rejected because recovery must not infer publication intent.
- Candidate cleanup was rejected because deletion authority is outside this simulation.

## Removal and next dependency

Remove the module, test, and report. Temporary pytest directories are disposable. Phase 4FT should
audit multiple simultaneous readers against stable current and orphan-candidate states.
