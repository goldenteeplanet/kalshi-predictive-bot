# Phase 3A Decision UI

Phase 3A adds a local web UI for reviewing opportunities before any future demo execution workflow.

## Run

```bash
kalshi-bot ui
```

Open:

```text
http://127.0.0.1:8080
```

Defaults:

- Host: `127.0.0.1`
- Port: `8080`
- `UI_READ_ONLY=true`
- `EXECUTION_ENABLED=false`
- `EXECUTION_DRY_RUN=true`
- Environment badge: `DEMO ONLY`

## Dashboard

The dashboard shows top local opportunity rankings:

- opportunity score
- estimated edge
- model name
- market title
- side and price
- spread
- liquidity score
- time to close
- model confidence
- existing paper position
- demo execution status

Actions include view details, paper trade, demo dry-run, and execution review. Demo execute controls are disabled unless all execution safety settings and risk checks allow them.

## Opportunity Detail

The detail page shows market rules, latest orderbook summary, forecast history, model component probabilities, pretty-printed feature JSON, score breakdown, paper P&L, backtest history, recent snapshots, simulated fills, and risk checks.

## Execution Review

The execution review page shows:

- full pre-trade checklist
- risk pass/fail list
- order preview
- `DEMO ONLY` badge
- dry-run badge
- kill switch status
- confirm demo execution button only when allowed

## Safety

The UI never enables production live trading. It does not display secrets. If `EXECUTION_ENABLED=false`, execution controls are disabled with an explanation. If `EXECUTION_DRY_RUN=true`, demo execute returns a dry-run result only. A typed confirmation token is required for the gated demo execute path.

## Reports

Header links point to local report files when present:

- `reports/opportunities.md`
- `reports/model_leaderboard.md`
- `reports/model_tournament.md`
- `reports/execution_report.md`

## Notes

The UI reads local SQLite data only. It does not call cloud services, does not authenticate with Kalshi, and does not place real orders.
