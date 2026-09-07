# Phase 4CJ — Stale Snapshot Refusal Acceleration

Phase 4CJ moves the freshness refusal decision ahead of downstream snapshot evaluation. A supplied, hash-protected batch is classified deterministically against a supplied evaluation timestamp and explicit maximum age.

The exact boundary is inclusive: `age_ms <= max_age_ms` is accepted, while one millisecond beyond it is refused. Future-dated snapshots are separately refused. Integer timedelta arithmetic avoids floating-point boundary drift. Only accepted snapshots set `downstream_evaluation_required=true`, making the avoided work explicit without executing that work.

Inputs require exact fields, unique identifiers, valid 64-character hexadecimal content hashes, UTC timestamps, and a canonical artifact hash. Reports preserve input order, record exact ages and reasons, and receive their own canonical hash. Publication is atomic.

The command has no database, network, exchange, service-control, or production-writer capability. It never authorizes execution or creates a production record.
