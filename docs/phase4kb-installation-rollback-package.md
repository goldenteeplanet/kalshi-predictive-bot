# Phase 4KB — Installation rollback package

Phase 4KB defines an integrity-bound, non-executable rollback manifest with exact ordered operations: disable and remove the proposed startup task, restore the previous signed configuration, preserve incident and restart history, then verify task and supervisor absence.

Operation drift, missing steps, duplicate or unknown operations, identical installed/previous snapshots, evidence-deletion semantics, execution requests, incomplete evidence, malformed fields, and tampering fail closed. Incident and restart-history hashes are retained as protected rollback evidence.

`READY` certifies the package manifest only. It does not run rollback or authorize task mutation, restart, service control, or execution. The builder is read-only and has no filesystem, Task Scheduler, process, database, notification, or host-control surface.
