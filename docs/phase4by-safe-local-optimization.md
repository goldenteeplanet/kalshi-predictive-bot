# Phase 4BY — Safe Local Computation Optimization

Phase 4BY removes one unnecessary intermediate active-event list from the Phase 4BW offline
workload. Filtering now feeds the existing deterministic sort through a generator. Sorting keys,
normalization fields, canonical serialization, output hashes, validation order, and refusal behavior
remain unchanged.

The equivalence proof runs the reference and optimized implementations over every Phase 4BV
fixture, requires byte-identical output hashes, and fails closed on any divergence. It validates the
same hash lineage and authority boundary before either implementation runs. This is a local offline
allocation reduction only; it does not change production code, records, services, or trade authority.
