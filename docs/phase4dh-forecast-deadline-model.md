# Phase 4DH — Forecast Deadline Model

Phase 4DH deterministically assigns each supplied forecast the earlier of two deadlines:
the evidence observation time plus its freshness window, or market close minus explicit
ranking, risk-review, and publication budgets. Equal deadlines identify both constraints.

Assignments retain microsecond precision, are ordered by deadline then forecast ID, and
explicitly expose an already elapsed deadline rather than extending it. Impossible source
timelines, closed markets, invalid durations, duplicate identities, malformed contracts,
and input tampering fail closed.

This phase only models deadlines; Phase 4DI owns refusal based on remaining compute time.
The report is deterministic, hash-protected, atomically published, and has no database,
network, forecast creation, exchange, service-control, or trading capability.
