# Phase 4JX — Supervisor heartbeat artifact

Phase 4JX defines a canonical Windows-side supervisor heartbeat bound to one supervisor instance, boot identity, exclusion lock, and configuration. Sequence and observation time must advance monotonically; instance or boot changes require a new artifact rather than silent continuation.

Dry-run is the default. Explicit writes are limited to an absolute `.json` target beneath an allowed root and use a create-only temporary file, `fsync`, and atomic replacement. Existing malformed, oversized, symlinked, incomplete, non-monotonic, or tampered state fails closed. Tests mutate only pytest temporary directories.

The heartbeat reports supervisor state only. It grants no task activation, restart, service-control, or execution authority and has no database, Task Scheduler, notification-provider, subprocess, or host-control surface.
