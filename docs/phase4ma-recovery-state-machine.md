# Phase 4MA — Recovery State Machine and Interruption Resume

## Outcome

Phase 4MA validates an ordered, hash-chained disaster-recovery flow from declaration through closure.
Every checkpoint binds the Phase 4LZ incident and recovery epoch, carries the complete fail-closed
runtime/trading invariant snapshot, and accumulates revocation, rotation, replay, quorum, and
after-action proof flags.

## Resume invariants

Exact checkpoint replay is idempotent; conflicting replay refuses. Bounded resume tokens bind a
verified checkpoint, explicit rollback target, incident, epoch, nonce digest, validity window, and
invariant snapshot. Skips, reversal, stale or replayed tokens, lost cumulative proof, post-terminal
changes, and authority broadening fail closed.

## Reproducible flow evidence

The uninterrupted fixture closes with `PASS` and flow SHA-256
`36566c86a0f058ed8392daebcf6cee3d02c0b0e3051634c9213d5a9dbc188d6a`. The bounded resume fixture
validates its checkpoint and rollback target, closes with `PASS`, and produces SHA-256
`e136a552a2743765e98778a5b193a5c742b502ee4f6756e5254f3142a1a9bacf`.

## Safety and removal

The state machine operates only on supplied values. It cannot persist checkpoints, activate policy,
access media, modify runtime, restart WSL or services, use a network, or create orders. Remove the
script, focused test, and report to roll back.

## Next phase

Phase 4MB — Recovery checkpoint corruption localization and minimal-repair planner.
