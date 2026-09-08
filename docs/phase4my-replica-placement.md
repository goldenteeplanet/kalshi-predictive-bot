# Phase 4MY — Replica Placement and Correlated-Failure Proof

## Outcome

Phase 4MY implements a deterministic, capacity-aware replica planner across provider, region, zone,
power, and hidden-dependency domains. Every accepted archive receives three shards whose placement
survives any single modeled domain failure with at least two remaining shards.

## Capacity and concurrency boundary

Maintenance drains, capacity exhaustion, hidden shared dependencies, stale reservation generations,
and non-survivable placement refuse. Jobs are ordered by priority and stable archive identity.
Concurrent reservations use generation comparison, partial allocations preserve durable decisions,
and replanning is deterministic.

## Reproducible evidence

- complete Phase 4MP–4MY suite: 99 passed
- modeled single-domain failure scenarios: 15
- placement-plan SHA-256: `bcea27e6d24b93d7a0bdf733e796d2c7bd8e2bbcbb531247d5aff6533eb764fc`
- survivability SHA-256: `fbc9a749218c2082be85ed4b42286b77a03a8d5f41937673a66224781cf2207f`
- concurrent-reservation SHA-256: `cd09b8bcf2dd1a28b996579488dcf3210d9a1d028dd824016a924618cb20858d`
- partial-allocation recovery SHA-256: `9cd9367e88713db0b57df68e9bdab02239ee1025fd430f944fa6e438fd70cc8b`

## Safety and removal

The planner mutates only copied in-memory inventories. It performs no infrastructure, filesystem,
network, runtime, service, acceptance, or order operation. Remove the three phase files to roll back.

## Next phase

Phase 4MZ — Offline adversarial-backtest harness and leakage firewall.
