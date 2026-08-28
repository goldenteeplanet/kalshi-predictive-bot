# Phase 4ID — Network-failure non-restart rule

Phase 4ID applies an unconditional non-restart policy to captured DNS, TCP timeout, TLS, HTTP 5xx, route-unreachable, and connection-reset evidence. It inspects no live endpoint and performs no network operation.

Coherent failures require bounded retry policy and operator alerting. Contradictory, unknown, stale, incomplete, future, malformed, or tampered evidence fails closed. The exact 120-second freshness endpoint passes.

No network outcome is restart-eligible. Network evidence cannot authorize recovery, service control, WSL or Windows restart, order creation, or execution.
