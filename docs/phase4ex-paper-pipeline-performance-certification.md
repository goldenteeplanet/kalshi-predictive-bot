# Phase 4EX — Paper Pipeline Performance Certification

Phase 4EX certifies paired deterministic baseline and optimized paper-pipeline measurements.
Every scenario must preserve behavior and avoid latency regression, and the aggregate integer-
microsecond improvement must meet an exact basis-point threshold. The calculation uses all
paired scenarios and has deterministic boundary behavior; wall-clock timing is never a
correctness gate.

Certification additionally requires hash-bound Phase 4EU equivalence, Phase 4EV mutation-
scanner, and Phase 4EW air-gap acceptance proofs. Any failed prerequisite, behavior drift,
single-scenario regression, malformed measurement, missing scenario, duplicate ID, invalid
hash, or insufficient aggregate improvement refuses certification with stable reasons.

The certificate never enables or authorizes paper-order creation. It creates no order, does
not mutate the production database, controls no service, and contacts no exchange. Its only
write is atomic publication of the explicitly requested local report artifact.
