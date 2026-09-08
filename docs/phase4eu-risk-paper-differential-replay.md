# Phase 4EU — Risk/Paper Differential Replay

Phase 4EU compares paired synthetic legacy and optimized risk/paper decisions. Equivalence
requires exact eligibility, quantity, all risk caps, and normalized reason codes. A mismatch
closes the certification gate and emits stable per-case, per-field reasons; it never selects
one path as authoritative or silently tolerates drift.

The replay validates eligibility/quantity consistency, typed cap boundaries, unique reason
codes, unique case IDs, complete schemas, source hashes, canonical case ordering, and input
integrity. Reason order is semantically normalized, while every substantive difference is
preserved in contract order.

This phase consumes only supplied synthetic artifacts. It cannot grant paper eligibility,
create or route an order, contact an exchange or service, or read or write a database. Its
only write is atomic publication of the explicitly requested local report.
