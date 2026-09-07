# Phase 4DW — Performance Regression Gate

Phase 4DW gates deterministic work, allocation, and parse units against both absolute and
percentage increase thresholds. Percentage comparisons use integer cross multiplication
in basis points, avoiding floating-point and rounding ambiguity. Exact boundaries pass;
one unit or basis point beyond them fails.

A zero baseline permits no percentage increase, even if an absolute allowance exists.
Logical output hashes must remain identical regardless of improved performance metrics.
Invalid metrics or policies, malformed contracts, and tampering fail closed.

Wall-clock time is explicitly excluded from the gate. The deterministic atomic report
creates no production record and has no database, network, exchange, service-control, or
trading capability.
