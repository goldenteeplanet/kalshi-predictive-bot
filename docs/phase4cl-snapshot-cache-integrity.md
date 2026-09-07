# Phase 4CL — Snapshot Cache Integrity

Phase 4CL verifies a supplied cache manifest without opening, reading, or populating a cache. Each cache key is independently derived from a namespace and identity using canonical hashing. Each value hash is independently recomputed from the supplied value.

The verifier detects cache-key mismatch, value-hash mismatch, duplicate keys, and a single semantic identity mapped to divergent keys. Integrity failures produce `FAIL_CLOSED` and `cache_usable=false`; malformed envelopes and digest formats are rejected. Mapping key order does not affect identity keys. Inputs are bounded to 10,000 entries.

The report records independent calculations, reasons, source hash, and its own canonical hash. Publication is atomic. The tool performs zero cache reads and writes and has no database, network, exchange, service-control, or production-writer surface.
