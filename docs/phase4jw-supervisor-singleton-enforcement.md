# Phase 4JW — Supervisor singleton enforcement

Phase 4JW requires two independent singleton layers: Task Scheduler's `IGNORE_NEW` multiple-instance policy with maximum concurrency one, and the atomic exclusion lock from Phase 4JP with one proven owner.

Policy drift, any maximum other than one, a non-required lock, ambiguous ownership, partial evidence, malformed data, or tampering fails closed. A second task instance is always denied; no stop-and-replace behavior is allowed.

`ENFORCED` is configuration evidence only. It grants no task activation, restart, service-control, or execution authority, and the evaluator performs no Task Scheduler, lock, filesystem, process, database, notification, or host mutation.
