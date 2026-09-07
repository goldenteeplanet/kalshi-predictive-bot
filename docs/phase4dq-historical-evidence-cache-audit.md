# Phase 4DQ — Historical Evidence Cache Audit

Phase 4DQ audits supplied historical cache-entry artifacts without consuming them. An
entry is eligible only when its source and series identities match, its artifact hash is
valid, its observation and availability are no later than the decision instant, its age is
within the inclusive freshness boundary, and its invalidation generation is current.

Observation or availability one microsecond after the decision instant is a no-lookahead
violation. Availability exactly at the decision instant is allowed. Schema drift, stale
evidence, tampering, invalidation changes, impossible timelines, duplicate IDs, and
malformed outer evidence fail closed or produce explicit ineligibility findings.

The deterministic report is atomically published and reads or writes no connected cache.
The module has no database, network, exchange, service-control, or trading capability.
