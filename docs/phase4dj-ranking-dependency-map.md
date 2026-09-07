# Phase 4DJ — Ranking Dependency Map

Phase 4DJ defines a complete artifact-only ranking contract. Every score dependency must
be present exactly once with an artifact hash, observation time, maximum age, and both
source-change and source-staleness invalidation triggers. A dependency is fresh through
the inclusive age boundary and refuses ranking one second beyond it.

Sort keys are ordered, unique, explicit about direction and null handling, and require a
final non-null `candidate_id ASC` total tie breaker under stable sorting. The report adds
ranking-spec, sort-key, and candidate-set invalidation triggers and exposes stale inputs.

Malformed or incomplete dependencies, future evidence, missing tie semantics, duplicate
fields, and hash tampering fail closed. Atomic publication creates no ranking and the
module has no database, network, exchange, service-control, or trading capability.
