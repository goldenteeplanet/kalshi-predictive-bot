# Phase 4CX — Bounded Batch Planner

Phase 4CX plans fixture batches in earliest-deadline-first order with fixture identifier as the deterministic tie-breaker. Admission jointly enforces maximum item count, serialized bytes, peak memory, rate-window units, and projected completion deadline.

Items inside a batch are modeled as parallel, so batch duration is the maximum item duration; batches execute sequentially in the plan. Exact limits pass. Any item that cannot fit by itself, exceeds remaining rate capacity, or cannot meet its projected deadline is explicitly refused with a stable reason rather than silently dropped.

Inputs and reports are exact-schema, bounded, deterministic, hash-protected, and atomically published. This is a planner only: it performs no requests or trading actions and has no database, network, exchange, service-control, order-creation, or production-writer capability.
