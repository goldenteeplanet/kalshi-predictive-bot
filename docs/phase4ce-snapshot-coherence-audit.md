# Phase 4CE — Snapshot Coherence Window Audit

Phase 4CE measures canonical UTC timestamp skew across explicitly related market groups. Every group
declares an exact millisecond window. Skew equal to the window is coherent; a one-millisecond excess
makes that group ineligible for offline evaluation.

The audit reports group-level skew, coherence, eligibility, incoherent group identifiers, and
ungrouped snapshots. Duplicate identities, missing group members, malformed or timezone-naive
timestamps, unordered group membership, invalid windows, and tampering fail closed. It changes no
snapshot, database, service, collector, exchange setting, or execution authority.
