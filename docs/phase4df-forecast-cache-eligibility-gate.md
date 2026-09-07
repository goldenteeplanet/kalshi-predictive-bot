# Phase 4DF — Forecast Cache Eligibility Gate

Phase 4DF makes an artifact-only cache decision from an exact request and hash-protected
candidate records. Reuse requires one and only one candidate matching market, ticker,
model identity and hash, feature-set hash, evidence hash, evidence freshness, inclusive
validity bounds, determinism, and completeness.

No eligible candidate produces `RECOMPUTE`; multiple eligible candidates are ambiguous
and produce `REFUSE`. Malformed contracts, invalid hashes, impossible timelines, and
tampering fail closed before a report is published. The report exposes every rejection
reason and never silently falls through to reuse.

The gate writes no cache entry and creates no forecast. Its deterministic report is
published atomically, and the module has no database, network, exchange, service-control,
or trading capability.
