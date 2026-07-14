# Phase 3D: Trader Workstation And Position Management

Phase 3D turns the local UI into a paper-trading workstation. It keeps the bot in paper/demo mode while giving you portfolio, position, model, market, watchlist, alert, and analytics views for overnight review.

## What It Adds

- `position_history`: append-only paper position snapshots by ticker.
- `portfolio_snapshots`: portfolio-level exposure and P&L snapshots.
- `watchlists` and `watchlist_markets`: local market watchlists.
- `alerts` and `alert_events`: local alert rules and triggered events.
- CLI commands:
  - `kalshi-bot portfolio-summary --output reports/portfolio_summary.md`
  - `kalshi-bot daily-briefing --output reports/daily_briefing.md`
  - `kalshi-bot analytics-report --output reports/analytics_report.md`
- UI pages:
  - `/portfolio`
  - `/positions/{ticker}`
  - `/models`
  - `/markets`
  - `/analytics`
  - `/watchlists`
  - `/alerts`

## Workstation Layout

The dashboard now uses a workstation layout:

- left navigation for the major review surfaces,
- center panel for the highest-signal opportunities and market monitor,
- right rail for alerts, model leaderboard, and paper positions.

The goal is to make nightly review faster: first check portfolio health, then alerts, then model performance, then the market monitor and position details.

## Paper Learning Workflow

Run the overnight paper loop, then generate the workstation reports:

```bash
kalshi-bot overnight-once
kalshi-bot portfolio-summary --output reports/portfolio_summary.md
kalshi-bot daily-briefing --output reports/daily_briefing.md
kalshi-bot analytics-report --output reports/analytics_report.md
kalshi-bot ui
```

Open `http://127.0.0.1:8080/portfolio` after the run. The workstation reads local paper orders, fills, positions, snapshots, rankings, and model leaderboard rows. It does not query private Kalshi account data.

## Alerts

Default local alert rules are created on first use:

- high opportunity score,
- high model confidence,
- widened spread,
- market expires soon,
- paper exposure limit.

Alerts are review prompts. They do not place, cancel, or modify orders.

## Watchlists

Default local watchlists are created on first use:

- Default Watchlist,
- High Conviction,
- Crypto,
- Weather,
- Sports.

Use `/watchlists` to add or remove ticker rows. Watchlists are local database records and do not sync to Kalshi.

## Limits

Phase 3D is still paper-only and demo-only. It adds workstation visibility and local management records, not live execution, authenticated portfolio access, real balances, or production order management.
