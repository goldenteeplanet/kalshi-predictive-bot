# Phase 4PC — Durable Alert Outbox and Crash-Window Proof

## Outcome

Phase 4PC models a hash-bound, dual-copy alert outbox with a prepared recovery journal. Recovery
selects the newest coherent checkpoint and applies a journaled delivery event exactly once. It
preserves the complete critical alert and its Phase 4PB state through interruptions before the
journal, after the journal, after either checkpoint copy, and after journal clearing.

Torn writes, stale replicas, corrupted journals, same-generation concurrent-writer divergence,
outbox/state disagreement, duplicate recovery, and reordered delivery events are tested. One good
replica repairs availability; ambiguity or loss of every valid copy fails closed.

## Safety and removal

The proof is deterministic and in-memory. It writes no runtime file, sends no notification,
restarts no service, creates no order, and cannot enable paper, demo, live, or autopilot execution.
Remove the three Phase 4PC files to roll back.

## Next phase

Phase 4PD — Alert outbox retention, compaction, tombstone, and audit-lineage proof.
