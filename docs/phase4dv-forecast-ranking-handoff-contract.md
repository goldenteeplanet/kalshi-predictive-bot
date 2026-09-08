# Phase 4DV — Forecast-to-Ranking Handoff Contract

Phase 4DV converts one exact, hash-protected forecast batch into a minimal ranking handoff.
Model, evidence, batch, generation, and deadline lineage appear once in a shared header;
candidate rows contain only ranking-required identity, normalized probabilities, and the
forecast hash. Extra fields, including repeated lineage, are ambiguous and fail closed.

Candidates are independently hash-verified, uniquely identified, and canonicalized by ID.
The compact handoff is proven lossless for the ranking view with matching reconstruction
hashes. Generation may equal the ranking deadline but cannot follow it.

The source is parsed once and the report is atomically published. It creates no ranking and
has no database, network, exchange, service-control, or trading capability.
