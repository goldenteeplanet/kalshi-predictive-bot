# Phase 4IV — Recovery timeout propagation

Phase 4IV propagates a parent recovery-plan deadline into cumulative child-step deadlines. Each step has a positive bounded duration, child deadlines are monotonic, and no child may exceed the parent. An exact budget fit passes.

Expired or future-issued parents, empty or incomplete steps, duplicate step codes, excessive step count, an over-budget request, malformed data, or tampering fails closed. Reaching the parent deadline is expired.

The propagator is deterministic and read-only. It starts no timer or operation and grants no recovery, service-control, host-restart, order, or execution authority.
