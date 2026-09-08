# Phase 4DC — Incremental Feature Computation

Phase 4DC provides a pure offline feature-DAG evaluator. It validates an exact,
hash-protected input contract, verifies that the supplied previous state exactly equals
a fresh baseline evaluation, computes the transitive dependent closure of changed source
nodes, and recomputes only that closure.

The incremental result is independently compared with a complete post-update baseline.
Any stale prior state, malformed graph, missing dependency, cycle, invalid decimal,
division failure, hash mismatch, or equivalence difference refuses publication. Decimal
arithmetic and canonical rendering avoid binary floating-point drift.

The output records recomputed and reused node IDs, matching result hashes, and explicit
non-execution fields. Publication is deterministic and atomic. The module has no database,
network, cache, exchange, service-control, forecast-creation, or order-creation capability.
