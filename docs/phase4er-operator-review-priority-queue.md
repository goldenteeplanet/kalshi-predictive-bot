# Phase 4ER — Operator Review Priority Queue

Phase 4ER constructs a deterministic local queue from hash-bound Phase 4EQ-style review
packets. Queue priority is lexicographic and explicit: earliest expiration, highest expected
value, freshest evidence, lowest review cost, then candidate ID as a stable final tie-breaker.

Only eligible, unexpired candidates requiring review enter the queue. Every excluded
candidate receives all applicable deterministic reason codes. Canonical millisecond UTC
timestamps define exact expiration and freshness boundaries; future evidence, duplicate
candidates, malformed fields, noncanonical timestamps, invalid hashes, and input tampering
fail closed. Input order cannot affect queue positions.

The queue is advisory and non-authorizing. It records no approval, creates no order, contacts
no service or exchange, and reads or writes no database. Its only write is atomic publication
of the requested local status artifact.
