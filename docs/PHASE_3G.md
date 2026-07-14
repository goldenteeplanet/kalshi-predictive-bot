# Phase 3G: Signal Marketplace

Phase 3G turns model behavior into named, trackable signals. It answers the practical paper-trading question: which signals are producing forecasts, opportunities, trades, and paper/demo P&L?

It does not add live trading, real-money trading, authenticated account access, or production execution.

## Built-In Signals

- Weather Signal
- Crypto Signal
- Economic Signal
- Market Divergence Signal
- Liquidity Signal
- Spread Compression Signal
- Momentum Signal
- Ensemble Agreement Signal
- Opportunity Score Signal
- Fresh Data Signal

Signals are registered in the `signals` table and can be extended with new definitions.

## Attribution

Forecast attribution is written when forecasts are generated through the model registry or `collect-once`.

Paper-trade attribution is written when a paper order is created. If a paper order is linked to a forecast, the order inherits the forecast's active signals.

Performance is refreshed when paper P&L is calculated and when signal reports, leaderboards, or explorer commands run.

## Tables

- `signals`
- `signal_events`
- `signal_forecasts`
- `signal_trades`
- `signal_performance`

## CLI

```bash
kalshi-bot signal-explorer
kalshi-bot signal-leaderboard
kalshi-bot signal-performance --signal-name "Crypto Signal"
kalshi-bot signal-report --output reports/signal_report.md
```

## UI

- `/signals` shows the Signal Marketplace cards and leaderboard.
- `/signals/{signal_name}` shows description, performance, counts, ROI, win rate, recent opportunities, recent trades, recent markets, top/worst markets, and research summary.
- Opportunity cards show the top three active signal badges.

## Research Integration

The Research Assistant now includes signal-aware primary drivers and supporting signals. For example, a writeup can call out `Crypto Signal`, `Market Divergence Signal`, `Liquidity Signal`, or `Ensemble Agreement Signal` as local evidence.

## Recommended Local Flow

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot signal-explorer
kalshi-bot signal-leaderboard
kalshi-bot signal-report --output reports/signal_report.md
kalshi-bot ui
```

## Limitations

- Attribution is deterministic and based on stored local evidence.
- Signal P&L is paper/demo only and may attribute the same market outcome to several active signals.
- Signals with small sample sizes stay visible as `Insufficient Data`.
- Signal rankings are diagnostic and not live-trading instructions.

## Database Hardening Addendum

A later prompt also used the Phase 3G label for the PostgreSQL migration layer and
SQLite hardening work. That database work keeps the signal marketplace intact and
adds:

- SQLite and PostgreSQL backend detection through `DB_BACKEND` and `DATABASE_URL`.
- Safer SQLite defaults: WAL mode, `busy_timeout=30000`, and `synchronous=NORMAL`.
- PostgreSQL SQLAlchemy pooling defaults suitable for overnight paper-learning runs.
- Alembic scaffolding with `kalshi-bot db-migrate` and `kalshi-bot db-revision`.
- Operator commands for `db-health`, `db-doctor`, `sqlite-backup`,
  `sqlite-recover`, and `migrate-sqlite-to-postgres`.
- Dashboard and `/settings/database` database health cards.

See `docs/POSTGRES_SETUP.md` and `docs/DATABASE_RECOVERY.md` for the operator
flows.
