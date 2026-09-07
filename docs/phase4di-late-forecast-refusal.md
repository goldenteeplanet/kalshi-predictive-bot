# Phase 4DI — Late Forecast Refusal

Phase 4DI compares each supplied Phase 4DH compute deadline with a conservative envelope:
estimated computation plus uncertainty plus an explicit safety margin. The exact boundary
is acceptable; one microsecond less is refused. An already elapsed deadline is reported
separately from a positive but insufficient window.

Durations are bounded nonnegative integers and all arithmetic is exact in microseconds.
Invalid durations, aggregate overflow, duplicate forecast IDs, malformed timestamps,
missing deadline lineage, and input tampering fail closed. Decisions are deterministically
ordered and expose required, remaining, and slack values.

Acceptance authorizes only offline computation, not forecast persistence or trading. The
atomic hash-protected report has no database, network, exchange, service-control, order,
or connected forecast capability.
