# Phase 4KA — Safe configuration reload

Phase 4KA creates a deterministic reload plan only for a validly signed candidate that changes an exact allowlist of non-authority fields: observation interval, alert rate limit, and diagnostic record limit. Changes are canonical and bound to current/candidate configuration, signature decision, and rollback snapshot hashes.

Reload is denied while the supervisor is unhealthy or recovery is pending, or when atomic swap and rollback are unproven. Safety-policy changes, duplicate fields, change/hash contradictions, incomplete requests, malformed data, and tampering fail closed. Identical hashes with no changes produce `NO_CHANGE`.

`READY` certifies the plan only. It does not apply configuration or authorize task activation, restart, service control, or execution. The planner is read-only and has no filesystem, Task Scheduler, process, database, notification, or host-control surface.
