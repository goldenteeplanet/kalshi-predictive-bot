# Phase 4EF — Portfolio Snapshot Freshness Gate

Phase 4EF validates a supplied immutable portfolio snapshot before any expensive risk
calculation. It uses explicit UTC timestamps, a fixed evaluation time, maximum field age,
maximum cross-field observation skew, and one required snapshot version.

Age and skew thresholds are inclusive: equality passes, while one millisecond beyond a
threshold refuses. Future observations, stale fields, mixed versions, excessive skew,
malformed timestamps or hashes, duplicate identities, schema drift, and hash tampering fail
closed with deterministic reason codes.

The gate executes zero risk calculations, reads no portfolio or database, creates no risk
decision, and authorizes no execution. Its status artifact is published atomically.
