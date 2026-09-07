# Phase 4CH — Retry and Backoff Determinism

Phase 4CH simulates throttling, timeout, partial-page, malformed-response, success, and retry-exhaustion
paths with exact exponential backoff schedules. Captured retry-after values can extend—but never
exceed the global cap on—the deterministic delay.

Malformed responses are terminal. Partial pages retry only when the fixture explicitly proves the
operation idempotent; otherwise they fail as unsafe. The final allowed attempt has no subsequent
delay. Invalid limits, outcomes, retry headers, duplicate scenarios, tampering, and inconsistent
evidence fail closed. The simulator never sleeps, calls a provider, changes retry settings, writes a
database, controls a service, or authorizes execution.
