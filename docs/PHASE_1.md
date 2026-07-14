# Phase 1

## Objective

Build a safe, read-only forecasting foundation for Kalshi public market data.

## Deliverables

- Public market metadata sync.
- Public orderbook snapshot capture.
- Local SQLite database with raw JSON storage.
- Market-implied baseline forecaster.
- Settlement sync for evaluated outcomes.
- Calibration metrics and Markdown/CSV reports.
- CLI commands for setup, collection, forecasting, settlement sync, and reporting.
- Tests that avoid live network calls.

## Non-Goals

- Live trading.
- Paper trading ledger.
- Order placement.
- Private Kalshi endpoints.
- Authentication, signing, portfolio, balance, or position logic.
- Execution engine design.

## Success Criteria

- A fresh checkout can install with `python -m pip install -e ".[dev]"`.
- `kalshi-bot collect-once --status open --limit 100 --max-pages 1` stores market snapshots and baseline forecasts.
- Reports can be generated from locally stored forecasts joined to settlements.
- Tests, linting, and practical type checks pass.

## Known Blockers / Future Decisions

- Settlement endpoint coverage may need refinement if public API filters change.
- Forecasts are market-implied only and do not include features beyond prices.
- Snapshot scheduling is left to cron, Task Scheduler, or a future service wrapper.
- SQLite is the default; a production deployment may need Postgres or another durable store.

## Next Phase Preview

Phase 2 should add a paper trading ledger, simulated fills, and strategy evaluation without live execution.

