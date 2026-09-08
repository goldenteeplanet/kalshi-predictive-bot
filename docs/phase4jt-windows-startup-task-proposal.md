# Phase 4JT — Windows startup-task proposal

Phase 4JT produces a deterministic, operator-visible proposal for a Windows startup task. The only accepted trigger is `AT_STARTUP`; highest privileges, network dependency, activation requests, non-dry-run mode, partial evidence, malformed inputs, and tampering are refused.

The proposal binds the task name, supervisor artifact, configuration, and rollback-script hashes into separate configuration and rollback preview hashes. It does not emit executable task XML or invoke Task Scheduler.

`READY` leaves activation disabled and requires operator review. It grants no task creation, restart, service-control, or execution authority and contains no filesystem, process, Task Scheduler, notification, database, or host-control surface.
