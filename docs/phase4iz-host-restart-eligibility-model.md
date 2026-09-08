# Phase 4IZ — Host-restart eligibility model

Phase 4IZ provides a deterministic, read-only eligibility decision for the guarded Windows-restart policy. Eligibility requires an explicit `HOST_RESTART_REQUIRED` classifier result, failed component recovery, complete evidence, fail-closed trading, unchanged protected invariants, sole-writer proof, and confirmation that the host is not running tests.

Partial evidence yields `INCOMPLETE`. Every failed precondition yields `DENIED`. Inputs and decisions are integrity-bound, and malformed or tampered data raises a stable fail-closed error.

`ELIGIBLE` is not restart authorization. A five-minute warning, cancellation path, persistent intent record, cooldown and budget checks, loop-breaker decision, and later workstream gate remain mandatory. This phase contains no process, service-control, notification, filesystem, database, or restart execution surface.
