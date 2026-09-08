# Phase 4CU — Read-Only Shadow Benchmark

Phase 4CU replays representative supplied market-data fixtures through a baseline full-validation/indented-serialization path and an attested changed-only/canonical-compact candidate path. Both paths emit the snapshot identity, per-component statuses, and final verdict.

Every fixture requires exact output equality. The candidate may not exceed baseline deterministic work units, and at least one fixture must strictly improve. Work units combine validated-component count and serialized bytes, avoiding nondeterministic wall-clock timing as a certification input. Candidate carry-forward is allowed only through an exact hash-valid attestation for the previous fixture snapshot.

The comparison is deterministic, canonically hash-protected, and atomically published. It applies no runtime changes and has no database, network, exchange, service-control, or production-writer capability.
