# Phase 4MW — Transcript Retention, Tombstones, and Legal Holds

## Outcome

Phase 4MW models transcript archive identity, retention classes, expiry, immutable custody events,
legal holds and releases, and content-bound deletion tombstones. A tombstone changes only simulated
archive state; it never deletes physical data.

## Audit boundary

Premature deletion, active holds, duplicate tombstones, post-tombstone changes, clock rollback,
custody tampering, and incorrect deletion targets refuse. Concurrent hold/delete proposals use the
same predecessor, so the accepted hold makes the stale deletion refuse. Compacted custody histories
can be reconstructed exactly from a bound prefix.

## Reproducible evidence

- complete Phase 4MP–4MW suite: 77 passed
- archive-entry SHA-256: `e5c9b881c9b48de2044992eda174ab2f3190bbdcb78983e7b39d2768de32d95a`
- tombstone audit SHA-256: `992c1241f62562a4cead24113df7bc5c0312c7a2ba4ed3faa93a4b4c7673f9bf`
- hold/delete race SHA-256: `500ebe2833dee045bae6c6a3d1f5254987422d6580f68468722f8fc33ac70fd7`
- custody compaction SHA-256: `a9012cb811f6f46bf55ccecf5871de4e9c860098eb5fffff62e26a2e9046ecf9`
- audit reconstruction SHA-256: `b3e09beb0c7e7beb3bfaca8080933a6c41d28bd37724302d296fbbd436444aac`

## Safety and removal

Everything is copied and evaluated in memory. No files, databases, services, runtime settings, or
orders are touched. Remove the three phase files to roll back.

## Next phase

Phase 4MX — Archive replication, erasure-coding model, and corruption-repair proof.
