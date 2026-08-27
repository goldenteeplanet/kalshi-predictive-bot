# Phase 4HX — Failure persistence window

Phase 4HX prevents a single failure quorum snapshot from being treated as persistent. Persistence requires at least two valid Phase 4HW quorum decisions for the same dependency spanning at least 60 seconds, with the newest quorum no more than 120 seconds old. Both endpoints are inclusive.

Duplicate decisions, mixed dependencies, future decisions, invalid upstream decisions, insufficient duration, a single snapshot, or stale latest evidence fail closed. The result is deterministic, integrity-bound, read-only evidence and grants no recovery, service-control, WSL/Windows restart, order, or execution authority.
