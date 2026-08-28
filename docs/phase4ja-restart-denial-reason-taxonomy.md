# Phase 4JA — Restart denial reason taxonomy

Phase 4JA defines a closed, versioned taxonomy for guarded-restart denial. Known reasons are deduplicated, ordered by deterministic safety priority, and bound to the source decision and incident hashes. Unknown, duplicate, missing, or contradictory reason evidence is classified as `TAMPERED` with `EVIDENCE_TAMPERED` as the primary denial.

An eligible source with no denial reasons produces `NOT_DENIED`, not authorization. Every other result keeps restart denied, and no result grants restart, service-control, or execution authority.

The taxonomy is read-only and contains no process, filesystem, database, notification, service-control, or restart execution surface.
