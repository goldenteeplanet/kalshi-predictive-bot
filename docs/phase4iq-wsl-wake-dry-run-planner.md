# Phase 4IQ — WSL wake dry-run planner

Phase 4IQ produces a symbolic WSL wake plan bound to a valid Phase 4IP `WSL_WAKE` capability, a verified Phase 4IH status capture, and one hashed target distribution. A stopped target yields verify-stop, wake, and verify-liveness steps; an already-running target yields a no-op verification plan.

Installing, unavailable, or unknown states are denied. Wrong capabilities, incomplete evidence, binding mismatches, malformed data, or tampering fail closed.

The plan is read-only and dry-run-only. It never invokes `wsl.exe` and grants no wake, shutdown, service-control, host-restart, order, or execution authority.
