# Phase 4BA — Independent verifier CLI

Phase 4BA is a standalone verifier that first reconstructs and validates the complete Phase 4AZ
entry/checkpoint chain, then revalidates a hash-bound production identity using enforced SQLite
read-only URI mode and `PRAGMA query_only=ON`. A chain failure has precedence and prevents database
access.

The production identity artifact binds resolved path, device, inode, size, nanosecond mtime, schema
hash, and table-count hash. Metadata is captured immediately before and after the read-only schema
and count inspection. A changed window is classified as concurrent authoritative-writer activity;
identity and logical-state drift have separate deterministic reasons.

The CLI atomically emits a machine-readable `phase4ba.independent-verification-report.v1` and a
concise human-readable report that includes the same report hash and first failure. It contains no
database mutation, production writer lock, service control, exchange, forecast, or order capability,
and every outcome remains non-authorizing.

Focused tests cover valid chain reconstruction and current-state inspection, byte-identical database
preservation, chain-first refusal without database access, identity drift, state drift, artifact
tampering, timezone handling, human/machine report agreement, fixed precedence, and the static
read-only surface.

