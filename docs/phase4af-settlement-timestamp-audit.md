# Phase 4AF settlement timestamp audit

Phase 4AF is a read-only readiness audit. It can identify an admissible settlement timestamp,
but it never writes or canonicalizes one. Its only output is a new atomic, hash-protected JSON
artifact at the explicitly supplied output path.

## Precedence policy

The `phase4af.timestamp-precedence.v1` policy orders admissible evidence as follows:

1. An existing lineage-valid `settlements.settled_at` value.
2. A timestamp carried directly by a validated raw exchange settlement/result payload.
3. A timestamp in a separately supplied, hash-valid Phase 4AF settlement evidence artifact.

Market close, expected-expiration, expiration, database-update, ingestion, and observation times
are recorded as contextual, non-authoritative evidence. They describe scheduling or collection,
not necessarily the exchange's determination time, and are never silently promoted. Naive
timestamps are retained in the inventory but are not assigned a guessed timezone.

Conflicting authoritative timestamps, broken lineage, stale evidence, missing sources, malformed
timestamps, unknown schemas, and unknown input states all fail closed. A timestamp is ready for a
future separately authorized canonicalization phase only when exactly one normalized authoritative
time exists, lineage and freshness validate, the result is binary, and no evaluation has appeared.

## Artifact integrity

Rows are sorted by ticker and capture ID. Every evidence item has evidence and provenance hashes;
every row has a row hash; the artifact has deterministic rows and top-level hashes over canonical
JSON. Publication uses a destination-local temporary file, file `fsync`, atomic replacement, and
best-effort directory `fsync`. Existing output is refused unless `--replace` is explicit.

## Read-only invocation

```bash
PYTHONPATH=src python scripts/local/phase4af_settlement_timestamp_audit.py \
  --phase4ad-artifact /tmp/phase4ad.json \
  --phase4ae-artifact /tmp/phase4ae.json \
  --history-dir /tmp/phase4ac-history \
  --production-db /path/to/production.db \
  --output /tmp/phase4af.json \
  --evaluation-time 2026-08-25T22:30:00Z \
  --freshness-seconds 1800
```

An optional `--settlement-evidence-dir` may contain only
`phase4af.settlement-timestamp-evidence.v1` artifacts. Every artifact and row set must validate.
The production database is opened with SQLite URI `mode=ro` and `PRAGMA query_only=ON`.

Safety flags in every successful result explicitly state that no production or research database,
service, trading mode, order, or existing artifact was changed.
