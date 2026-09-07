# Phase 4BG — Scale and resource-boundedness audit

Phase 4BG measures synthetic row validation/order cost, history-chain construction, artifact
publication, traced memory, and rollback cost. Row, history, artifact-byte, and estimated-memory
limits are checked before allocation. All database work uses an in-memory synthetic table; atomic
publication uses an owned temporary directory that is removed after measurement.

The audit records nanosecond elapsed measurements and traced memory while separately proving stable
ordering, history-head, and artifact-content hashes across equivalent runs. Boundary tests exercise
the maximum supported 10,000 rows, 10,000 history links, and 2,000,000 artifact bytes; any larger,
negative, boolean, or estimated-over-memory request is refused.

Outputs are `phase4bg.scale-resource-audit.v1` and
`phase4bg.resource-bounds-proof.v1`. Focused tests cover each measured resource, deterministic hash
components, every exact limit/over-limit case, rollback, atomic publication, timezone handling, and
the synthetic-only static surface.

