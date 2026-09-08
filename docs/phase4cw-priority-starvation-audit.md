# Phase 4CW — Priority-Starvation Audit

Phase 4CW audits a supplied sequence of scheduling cycles against a maximum service wait. Every sliding window of that many cycles must include service for every market. If the trace is shorter than the bound, every market must still appear at least once. Equality at the service boundary passes; one additional unserved cycle fails.

Stale-state visibility is independent of service fairness. Every market declared stale in a cycle must appear in that cycle's surfaced-stale set; otherwise the audit refuses even when service frequency passes. This prevents priority ordering from hiding degraded state.

Inputs and outputs are exact-schema, bounded, deterministic, hash-protected, and atomically published. The audit produces no trading action and has no database, network, exchange, service-control, order-creation, or production-writer capability.
