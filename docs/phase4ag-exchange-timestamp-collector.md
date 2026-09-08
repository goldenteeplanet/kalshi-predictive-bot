# Phase 4AG exchange timestamp evidence collector

Phase 4AG collects evidence; it does not canonicalize settlement timestamps. It reads the Phase
4AD–4AF lineage and the production database, then publishes a collection-status artifact and a
Phase 4AF-compatible evidence artifact. Database access is SQLite URI `mode=ro` with
`PRAGMA query_only=ON`.

## Source and request policy

Hash-valid `phase4ag.raw-exchange-response.v1` archives have precedence. In explicit
`bounded-read-only-api` mode, the only allowed operation is an HTTPS `GET` to the configured exact
host and `/trade-api/v2/markets/{exact-ticker}` path. Redirects to another host, pagination,
oversized responses, unbounded retries, and unsafe base URLs are rejected. Credentials are not CLI
arguments and are never included in artifacts or diagnostics.

The allowlisted timestamp fields are `settled_at`, `settlement_ts`, `settlement_time`,
`determined_at`, `determination_ts`, and `result_ts`. A candidate must have an explicit timezone,
an exact ticker identity, and a result matching the current lineage-valid settlement. Market close,
expiration, retrieval, ingestion, and database update times are never promoted.

## Offline example

```bash
PYTHONPATH=src python scripts/local/phase4ag_exchange_timestamp_collector.py \
  --phase4af-artifact /tmp/phase4af.json \
  --phase4ad-artifact /tmp/phase4ad.json \
  --phase4ae-artifact /tmp/phase4ae.json \
  --history-dir /tmp/phase4ac-history \
  --production-db /path/to/production.db \
  --archive-dir /path/to/read-only-archives \
  --mode offline \
  --evaluation-time 2026-08-25T22:30:00Z \
  --status-output /tmp/phase4ag-status.json \
  --evidence-output /tmp/phase4ag-evidence.json
```

Both outputs carry a deterministic pair ID. Publication creates and `fsync`s both temporary files
before either final path is replaced, then performs best-effort directory synchronization. Existing
outputs are refused unless `--replace` is explicit. An empty evidence artifact is valid and remains
fail-closed with `safe_for_phase4af_reaudit=false`.
