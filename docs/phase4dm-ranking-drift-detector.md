# Phase 4DM — Ranking Drift Detector

Phase 4DM compares hash-protected baseline and current ranking snapshots. It identifies
ranking changes and attributes them to model identity, dependency hashes, arithmetic mode,
ordering specification, or stale-input state. A changed ranking with no such evidence is
explicitly classified as `UNATTRIBUTED_DRIFT` and is never treated as safe.

Snapshots require exact fields, valid hashes, contiguous ranks, unique candidate IDs, and
explicit freshness and arithmetic semantics. Metadata changes remain visible even when
the final ranking does not change. Malformed snapshots and input tampering fail closed.

The deterministic report is atomically published and creates no ranking. The module has
no database, network, exchange, service-control, or trading capability.
