# Phase 4BS — Latency Attribution Engine

Phase 4BS assigns every measured millisecond to compute, I/O, rate limiting, queueing, lock wait,
stale evidence, external API delay, or operator wait. Stage timestamps must be timezone-aware and
the cause buckets must exactly conserve each measured interval.

The output includes deterministic cause totals, integer basis-point shares, and a stable primary
cause ranking. Unknown, missing, reordered, negative, ambiguous, or non-conserving evidence fails
closed. Results are optimization candidates only and do not alter configuration or execution.
