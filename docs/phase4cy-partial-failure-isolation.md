# Phase 4CY — Partial-Failure Isolation

Phase 4CY builds synthetic source/market artifacts independently from supplied success and failure events. A source failure quarantines only artifacts from that source. A market failure quarantines only that source/market. A page failure quarantines the source/market artifact assembled from that page, preventing publication of an incomplete market snapshot.

Unrelated artifacts are assembled solely from their own sorted success events, so injecting a failure in another isolation domain cannot change their content hash. Tests compare those hashes directly against a no-failure baseline. Quarantined artifacts are omitted; partial artifacts are never emitted.

Inputs and reports are exact-schema, bounded, deterministic, hash-protected, and atomically published. All artifacts are synthetic report content: the phase performs zero production publications and has no database, network, exchange, service-control, or production-writer capability.
