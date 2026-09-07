# Phase 4CM — Weather Data Source Latency Audit

Phase 4CM audits supplied NOAA and related weather-source observations without network access. It measures request-to-response latency, data freshness at an explicit evaluation time, availability, declared timestamp meaning, and cross-source value spread.

Exact latency and freshness thresholds are inclusive and use integer millisecond arithmetic. Cross-source reconciliation occurs only when at least two sources are available and their timestamp meanings match. Observation time, issue time, and valid time are never treated as interchangeable. Missing availability, incompatible meanings, and value divergence receive distinct results.

Inputs are hash-protected, exact-schema, bounded to 10,000 samples, and require unique identifiers, UTC timestamps, finite numeric values, and explicit timestamp semantics. The deterministic report is hash-protected and atomically published. The tool makes zero network calls and has no database, exchange, service-control, or production-writer capability.
