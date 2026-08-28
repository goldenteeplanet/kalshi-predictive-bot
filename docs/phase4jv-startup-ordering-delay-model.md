# Phase 4JV — Startup ordering and delay model

Phase 4JV fixes startup ordering to: host boot, a 120-second delay, test-host prohibition, persistent-state integrity validation, singleton-lock acquisition, read-only health observation, and alert-only disposition. Stage order and delay are exact and integrity-bound.

Reordering, missing or duplicate stages, unknown stages, delay drift, recovery-on-boot requests, activation requests, partial evidence, malformed data, or tampering fail closed. The model uses no implicit clock and performs no wait.

`VALID` is configuration evidence only. Startup remains alert-only and grants no activation, recovery, restart, service-control, or execution authority. The model contains no Task Scheduler, filesystem, process, clock, notification, database, or host-control surface.
