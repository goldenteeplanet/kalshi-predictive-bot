# Phase 4EE — Portfolio Snapshot Minimality

Phase 4EE proves the smallest sufficient immutable portfolio snapshot for deterministic
Phase 3M and Phase 3N evaluation. Each explicit decision requirement names acceptable
hash-addressed fields. The tool exhaustively finds the minimum field set covering every
requirement and uses lexicographic field identifiers as the deterministic tie-breaker.

Both decisions must be covered. Mutable fields, malformed hashes, duplicate identities or
alternatives, missing references, oversized search spaces, schema drift, and hash tampering
fail closed. The report records selected and omitted fields plus requirement-level coverage,
making minimality independently auditable.

The tool consumes a supplied artifact; it performs zero portfolio reads, writes no database,
creates no risk decision or reservation, and publishes atomically.
