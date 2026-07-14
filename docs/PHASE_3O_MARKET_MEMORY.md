# Phase 3O: Market Memory

Phase 3O adds a durable, point-in-time learning ledger for market snapshots,
forecasts, opportunity decisions, sizing/risk gates, paper trade lifecycle
events, settlements, and final outcomes. It is memory-only: it does not change
forecast logic, thresholds, Phase 3M sizing, Phase 3N risk behavior, or any
paper/demo execution behavior.

## Repository Findings

- Market ingestion persists `markets` and `market_snapshots` through
  `data.repositories.insert_market_snapshot`.
- Forecast producers write `forecasts` through `insert_forecast`; failed
  forecast attempts are observable in the model registry loop.
- Opportunity scoring writes `market_rankings` and `market_opportunities`.
- Phase 3M writes `position_sizing_decisions`.
- Phase 3N writes `advanced_risk_decisions`.
- Paper trading writes `paper_orders`, `paper_fills`, `paper_positions`, and
  `paper_pnl`; settlements write `settlements`.
- The repo uses SQLAlchemy ORM with Alembic migrations. No durable outbox/event
  bus or object-store archive facility existed before this phase.
- Existing model metadata does not provide authoritative artifact hashes,
  training cutoffs, or code commits in all environments, so Phase 3O stores
  nullable lineage fields plus data-quality flags instead of fabricating them.

## Stores

`market_memory` captures decision-time and settlement-time market state.

`forecast_memory` captures forecast creation, failed forecasts, opportunity
scoring/rejection, Phase 3M sizing, Phase 3N risk decisions, trade selection,
no-trade decisions, and finalized forecast labels.

`trade_memory` captures paper trade intent, fills, positions, settlement
outcomes, P&L fields, and the paper fill model version.

All stores include event envelope fields: event id, aggregate id, event type,
sequence, schema version, event time, observed time, recorded time, source
component, idempotency key, payload hash, correction fields, metadata, quality
flags, and forward-compatible payload JSON.

## Idempotency And Corrections

Writers are deterministic and idempotent:

- same idempotency key and same payload hash returns the prior event receipt;
- same idempotency key and different payload hash writes to
  `memory_event_quarantine`;
- corrections must be appended with `is_correction` and
  `supersedes_memory_event_id` rather than overwriting history.

## Point-In-Time Learning

Use `kalshi-bot memory-dataset --training-as-of ...` to export learning rows.
The command requires a training cutoff and excludes labels finalized after that
cutoff. Open, pending, preliminary, and missing labels are not treated as final.
No-trade and risk-blocked forecast events remain available by default so future
training does not learn only from executed trades.

## Archive And Backfill

`kalshi-bot memory-archive` writes verified JSONL archives for all memory
stores, records checksums and row counts in `memory_archive_manifests`, and does
not purge hot data.

`kalshi-bot memory-backfill --dry-run` counts existing source rows. Use
`--write` to append backfilled memory events with explicit backfill provenance.
Backfilled/reconstructed records remain separated through ingestion mode and
event payload metadata.

## Commands

```bash
kalshi-bot memory-status
kalshi-bot memory-report --output reports/market_memory_report.md
kalshi-bot memory-backfill --dry-run
kalshi-bot memory-backfill --write
kalshi-bot memory-archive --output-dir data/memory_archive
kalshi-bot memory-dataset --training-as-of 2026-06-18T00:00:00Z --output reports/memory_dataset.json
kalshi-bot memory-timeline --forecast-id forecast:1
kalshi-bot trade-memory-timeline --trade-id paper_order:1
```

## Rollout

1. Run `kalshi-bot db-migrate`.
2. Run `kalshi-bot memory-status`.
3. Generate a report with `kalshi-bot memory-report`.
4. Run `kalshi-bot memory-backfill --dry-run`.
5. If counts look correct, run `kalshi-bot memory-backfill --write`.
6. Run overnight/learning normally. Phase 3O capture is shadow-only by default.

## Rollback

Set:

```bash
PHASE_3O_MARKET_MEMORY_ENABLED=false
```

or:

```bash
PHASE_3O_MARKET_MEMORY_MODE=disabled
```

This disables Phase 3O capture without changing trading, paper orders, Phase 3M,
Phase 3N, learning mode, or reports outside the memory subsystem.

## Known Gaps

- No durable transactional outbox exists yet; current hooks are synchronous,
  non-fatal shadow capture.
- Model artifact hashes and training cutoffs are not authoritative for all
  forecasters.
- Object storage lifecycle, restore drills, and multi-year partition plans are
  documented but not automated.
- Live trade lifecycle fields remain nullable because this repo remains
  paper/demo only.
