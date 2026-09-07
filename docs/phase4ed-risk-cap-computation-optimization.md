# Phase 4ED — Risk-Cap Computation Optimization

Phase 4ED computes reusable cap sets once and applies them to multiple synthetic candidates.
Every numerator and denominator is supplied as a Decimal string; division uses exact
Decimal arithmetic and explicit floor-to-integer semantics. A candidate's allowed quantity
is the minimum of its requested quantity and every cap term.

The report emits legacy and optimized quantities side by side and requires them to be
identical. Deterministic work units describe repeated versus memoized computation without
using wall-clock measurements as a correctness gate. Decimal boundaries, zero caps,
invalid denominators, non-finite values, duplicates, missing references, schema drift, and
hash tampering fail closed.

This is an offline artifact computation. It creates no risk decision, reservation, intent,
or order and publishes atomically without database or exchange access.
