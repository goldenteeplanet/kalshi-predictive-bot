# Phase 4DT — Bounded Local Parallel Executor

Phase 4DT executes only two fixed synthetic Decimal operations (`SUM` and
`SUM_OF_SQUARES`) in a bounded local thread pool. It cannot execute supplied code or
commands. Worker count, task count, values per task, and aggregate work units are checked
before the pool is created.

Parallel results are canonicalized by task ID and must equal an independently computed
sequential result item-for-item and by hash. Exact Decimal arithmetic avoids binary float
drift. Invalid flags, operations, values, identities, limits, contracts, and tampering fail
closed.

The executor launches no external process and handles only supplied synthetic values. Its
atomic report creates no task or forecast and has no database, network, exchange,
service-control, arbitrary-code, or trading capability.
