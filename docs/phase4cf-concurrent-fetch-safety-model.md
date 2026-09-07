# Phase 4CF — Concurrent Fetch Safety Model

Phase 4CF simulates bounded fetch slots, throttling, timeouts, cancellation, malformed responses,
retry exhaustion, and deterministic result merging. Operations may be supplied in any completion
order, but successful result hashes merge by their unique logical ordinal.

Concurrency and retry limits are explicitly bounded. Malformed responses and cancellations stop
locally; timeout and throttle outcomes consume the configured retry allowance; one failure cannot
corrupt unrelated successes. Duplicate identities or ordinals, invalid outcomes, inconsistent result
hashes, and tampering fail closed. The model performs no threads, tasks, API calls, collector changes,
database writes, service control, or execution.
