# Phase 4EN — Intent Expiration Gate

Phase 4EN computes a conservative required routing-completion timestamp from a supplied UTC
evaluation time, deterministic routing duration, and safety margin. Intent, evidence, and
operator approval validity must each extend through that timestamp.

Equality passes with zero headroom; a one-millisecond shortfall refuses. The report records
headroom for every expiration and emits stable intent, evidence, then approval reason order.
Strict UTC timestamps, nonnegative duration bounds, unique intent identities, lineage hashes,
schema integrity, and artifact hashes are enforced fail closed.

The gate attempts no routing, creates no paper order or record, has no connected-system
capability, and publishes only an atomic status artifact.
