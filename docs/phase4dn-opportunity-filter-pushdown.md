# Phase 4DN — Opportunity Filter Pushdown

Phase 4DN evaluates cheap, decisive rejection conditions before expensive ranking work.
Candidates may be rejected early only for a closed market, stale evidence, an existing
hard block, or an exact upper score bound strictly below the minimum score. Equality at
the threshold remains in the full-ranking set.

The audit also evaluates the complete baseline predicate from supplied fixture scores and
requires every baseline-eligible candidate to survive pushdown. A claimed upper bound
below the full score is invalid evidence and fails closed. The report records every reason,
survivor, rejection, false rejection, and avoided expensive computation.

This is synthetic, artifact-only validation. Atomic publication creates no ranking and the
module has no database, network, exchange, service-control, or trading capability.
