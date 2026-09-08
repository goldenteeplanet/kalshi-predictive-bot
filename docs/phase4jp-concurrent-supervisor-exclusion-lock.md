# Phase 4JP — Concurrent supervisor exclusion lock

Phase 4JP adds a bounded, atomic create-only supervisor lock beneath an explicit Windows-side root. Dry-run is the default. Explicit acquisition uses exclusive file creation and `fsync`, so only one contender can acquire a missing lock.

An existing valid lock yields `HELD`; corrupt, oversized, symlinked, malformed, partial, escaped, or wrongly suffixed state fails closed. Locks are never stolen, deleted, refreshed, or treated as stale by this module; reconciliation is required after owner failure. Tests mutate only pytest temporary directories.

The lock coordinates supervisors only and grants no restart, service-control, or execution authority. The implementation has no deletion, database, notification, subprocess, or host-control surface.
