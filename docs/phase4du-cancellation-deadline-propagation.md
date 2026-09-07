# Phase 4DU — Cancellation and Deadline Propagation

Phase 4DU models local task cancellation from supplied timestamps and dependency evidence.
A task finishing exactly at its deadline is publishable; starting at the deadline or
finishing one microsecond late cancels it. Cancellation or unpublishability propagates to
all dependents while independent completed work remains eligible.

Incomplete tasks cannot carry result hashes and never publish. Cancelled and late result
hashes are removed from the publication set. Missing links, cycles, duplicate edges,
impossible timelines, malformed contracts, and tampering fail closed.

This phase models local cancellation and launches no worker or process. The atomic report
publishes zero partial or late results and has no database, network, exchange,
service-control, task-record, or trading capability.
