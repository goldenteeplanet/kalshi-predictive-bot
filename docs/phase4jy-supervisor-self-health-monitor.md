# Phase 4JY — Supervisor self-health monitor

Phase 4JY evaluates integrity-bound supervisor heartbeat, loop, error, and lock-ownership evidence against explicit time. Heartbeats may be at most 180 seconds old, loop duration at most 60 seconds, and no more than two consecutive internal errors are tolerated before failure.

One or two errors or a slow loop yields `DEGRADED`; a stale heartbeat, lost lock, or more than two errors yields `FAILED`. Future timestamps are tampered; incomplete or unverified heartbeat evidence is incomplete. Every non-healthy state requires an alert.

The monitor never self-restarts or authorizes a host restart, service control, or execution. It is deterministic and read-only and uses no implicit clock, filesystem, notification provider, process, database, or host-control surface.
