# Phase 3C.5: Overnight Paper Learning

Phase 3C.5 adds a bounded overnight loop for collecting more data, making paper bets, and measuring whether the model stack is improving.

## What It Adds

- `overnight_runs`: one row per overnight run.
- `overnight_cycles`: one row per cycle, including market, snapshot, forecast, paper order, opportunity, settlement, report, and error counts.
- `model_iteration_metrics`: per-cycle model feedback rows for forecasts, opportunities, paper trades, P&L, average edge, and average opportunity score.
- `forum_consensus_signals`: imported aggregate longshot-consensus signals.
- CLI commands:
  - `kalshi-bot overnight-status`
  - `kalshi-bot overnight-once`
  - `kalshi-bot overnight-run`
  - `kalshi-bot overnight-report --output reports/overnight_report.md`
  - `kalshi-bot ingest-forum-consensus --input-file consensus.json`
- UI:
  - Dashboard overnight status card.
  - `/overnight` status page with plain-English sections and model metrics.
  - Forum consensus callouts on opportunities when a qualifying longshot signal exists.

## Safe Defaults

- `OVERNIGHT_ENABLED=false`
- `OVERNIGHT_INTERVAL_MINUTES=15`
- `OVERNIGHT_MAX_CYCLES=32`
- `OVERNIGHT_MODEL=ensemble_v2`
- `OVERNIGHT_RUN_PAPER=true`
- `OVERNIGHT_RUN_DEMO=false`
- `OVERNIGHT_RUN_BACKTEST=true`
- `OVERNIGHT_RUN_REPORTS=true`
- `OVERNIGHT_STOP_ON_ERROR=false`

The scheduler is disabled until explicitly enabled. `overnight-once` is available for a manual paper-learning cycle. The overnight loop does not submit demo or real orders by default.

## Learning Loop

Each cycle is designed to create measurable evidence:

1. Collect public open-market data.
2. Ingest crypto data if reachable.
3. Build crypto features and links.
4. Build weather features and links from stored weather data.
5. Run `forecast --model all`.
6. Refresh model tournament weights when enough data exists.
7. Run `forecast --model ensemble_v2`.
8. Scan opportunities.
9. Create paper bets.
10. Calculate paper P&L.
11. Sync settlements.
12. Generate reports.
13. Store `model_iteration_metrics`.

This is not automatic live trading. It is a paper feedback loop so mistakes can be inspected before trust increases.

## Forum Consensus

Forum consensus is imported from local aggregate JSON only. It is not scraped automatically.

A signal is highlighted when:

- the observation is recent,
- `winner_count >= FORUM_CONSENSUS_MIN_WINNERS`,
- `average_win_rate >= FORUM_CONSENSUS_MIN_WIN_RATE`,
- `longshot_price <= FORUM_CONSENSUS_LONGSHOT_MAX_PRICE`.

Qualifying signals show up as Forum Consensus / Longshot Watch in opportunity explanations. They are review signals, not execution triggers.

## Recommended Overnight Command

```bash
kalshi-bot overnight-status
kalshi-bot overnight-once
kalshi-bot overnight-report --output reports/overnight_report.md
OVERNIGHT_ENABLED=true kalshi-bot overnight-run
```

The default database remains `data/kalshi_phase1.db` so overnight metrics can join against the same forecasts, paper orders, settlements, and P&L. For an isolated experiment, set a different `KALSHI_DB_URL`.
