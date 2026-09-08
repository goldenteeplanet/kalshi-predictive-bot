# Phase 4JE — Six-hour restart cooldown

Phase 4JE evaluates an integrity-bound restart-history state against an explicitly supplied time. A prior restart blocks eligibility until exactly 21,600 seconds have elapsed. Verified empty history is clear; missing, incomplete, unverified, contradictory, future-dated, malformed, or tampered state fails closed.

The decision reports elapsed time, remaining time, and the next eligible timestamp deterministically. `CLEAR` is only cooldown evidence and does not authorize restart, service control, or execution.

The evaluator is read-only and performs no clock access, persistence, notification, process, service, or restart operation. Later phases must enforce the seven-day budget and loop breaker independently.
