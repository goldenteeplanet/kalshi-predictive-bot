# Phase 4FD — Fast CI Test Partitioning

The planner uses stable longest-processing-time assignment over recorded integer millisecond durations. Every test node appears exactly once in focused shards and again in a mandatory, complete pre-merge suite. Dependency identifiers must resolve, duplicate nodes and path traversal fail closed, and input order cannot change the plan. Partitioning is planning-only: it does not skip merge coverage or modify CI.
