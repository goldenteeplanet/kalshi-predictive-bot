# Phase 4EA — Risk-Decision Dependency Graph

Phase 4EA maps the immutable inputs, calculations, caps, hard blocks, ordering, and
artifact lineage feeding Phase 3M position sizing and Phase 3N advanced risk. It resolves
the graph topologically, records transitive ancestors and input lineage, calculates
deterministic work units, and identifies calculations shared by both decisions.

Exactly one position-sizing and one advanced-risk decision are required. Each decision
must depend transitively on at least one cap, one hard block, and immutable hash-addressed
input evidence. Missing links, cycles, duplicate identities or dependencies, malformed
hashes, mutable evidence, and invalid work units fail closed.

This artifact is descriptive and read-only. It creates no decisions or reservations,
authorizes no execution, accesses no database or exchange, and publishes atomically.
