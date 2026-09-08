# Phase 4MQ — Review-Workflow Concurrency, Race, and Crash Recovery

## Outcome

Phase 4MQ adds an in-memory compare-and-swap append model over Phase 4MP. Each durable proposal must
match the exact generation and head, then pass the complete workflow validator. Exact proposal
retries are idempotent; conflicting retries and stale concurrent transitions refuse.

## Race and crash proof

The simulator covers reviewer assignment, fix selection/evidence, crossing sign-offs,
close/withdraw, close/expire, and reopen/post-closure races. Only one proposal at a shared CAS point
can win. Prepared events are discarded fail-closed; crash matrices at every append preserve every
durable event and resume from the exact head.

Deterministic evidence (evaluated at `2026-08-29T01:00:00Z`):

- focused Phase 4MP/4MQ suite: 22 passed
- crash cut points: 11
- crash coverage SHA-256: `8a8a521f5b4cb7d9502d00a6b4e4c64cbc5d7371061f6e62d145b477237fbb37`
- recovery SHA-256: `85e2a7538978e1038772314cecf4fbf2e7b8ff4df5d85ae5c2c0c911bd29f22d`
- interleaving coverage SHA-256: `6d0caf2cf56525403dda2583f3b1b9c2c9c99e2fe29568af7e2277f73d397327`
- residual-risk SHA-256: `3baa532d9c5aa66ebf440cf51fb20058b61512834ad8f9411f519ca1e2161ff0`
- audit SHA-256: `feefb9b5f3ded3a9e01fc374da3ad5b13574210ee09d470b9e89640237dae227`

## Safety and removal

All histories and crashes are simulated in memory. No workflow is persisted; no package, network,
runtime, WSL, service, or order capability is used. Remove the script, focused test, and report to
roll back.

## Next phase

Phase 4MR — Review-workflow long-history compaction and audit-anchor proof.
