# Phase 3B Demo Autopilot And Guardrails

Phase 3B adds a scheduled local autopilot for demo-only opportunity selection and dry-run execution review. It does not add production live trading, real-money order routing, authenticated Kalshi trading, portfolio access, or secret handling.

## Defaults

- `AUTOPILOT_ENABLED=false`
- `AUTOPILOT_DRY_RUN=true`
- `AUTOPILOT_MODEL=ensemble_v2`
- `AUTOPILOT_INTERVAL_SECONDS=300`
- `AUTOPILOT_MAX_CYCLES=0`
- `AUTOPILOT_MAX_ORDERS_PER_CYCLE=1`
- `AUTOPILOT_MAX_DAILY_ORDERS=10`
- `AUTOPILOT_MIN_EDGE=0.05`
- `AUTOPILOT_MIN_OPPORTUNITY_SCORE=75`
- `AUTOPILOT_STOP_ON_DRAWDOWN=true`
- `AUTOPILOT_MAX_DAILY_DRAWDOWN=5.00`
- `AUTOPILOT_MAX_OPEN_DEMO_ORDERS=5`
- `AUTOPILOT_REQUIRE_FRESH_DATA_MINUTES=15`

## Commands

```bash
kalshi-bot autopilot-status
kalshi-bot autopilot-once
kalshi-bot autopilot-run
kalshi-bot autopilot-report --output reports/autopilot_report.md
```

`autopilot-once` creates one run and one cycle. `autopilot-run` uses `AUTOPILOT_INTERVAL_SECONDS` and `AUTOPILOT_MAX_CYCLES`; `0` means run until a guardrail stops it or the operator interrupts it.

## Cycle Flow

1. Confirm autopilot is enabled.
2. Confirm `KALSHI_ENV=demo`.
3. Confirm the execution kill switch is off.
4. Confirm non-dry-run mode is not used while execution is disabled.
5. Confirm the model is in the local allow-list.
6. Confirm daily order, open demo order, data freshness, and drawdown limits.
7. Run the selected forecast model over local snapshots.
8. Scan local opportunities.
9. Apply opportunity guardrails.
10. Record dry-run orders or call the Phase 3A demo executor.
11. Persist run, cycle, and risk-event summaries.

## Guardrails

Every block writes a `risk_events` row. Guardrails cover:

- autopilot disabled
- non-demo environment
- execution disabled when autopilot is not dry-run-only
- maximum daily orders
- maximum orders per cycle
- maximum open demo orders
- minimum edge
- minimum opportunity score
- stale or missing data
- duplicate ticker/model/side attempts
- daily drawdown
- kill switch
- model allow-list

## Tables

- `autopilot_runs`
- `autopilot_cycles`
- `risk_events`

## UI

The local UI includes `/autopilot` with current config, last run, last cycle, risk events, blocked orders, submitted demo orders, stop reason, a `Run one dry-run cycle` button, and a link to `reports/autopilot_report.md`.

No UI button is provided for live trading.

## Report

Generate the Markdown report with:

```bash
kalshi-bot autopilot-report --output reports/autopilot_report.md
```

The report includes current config, last run summary, recent cycles, risk events, attempted/submitted/blocked orders, stop reasons, and a recommended next action.

## Safety Notes

The autopilot uses local database state and the existing Phase 3A demo execution boundary. In dry-run mode it does not call the execution client. Fresh data collection remains an operator-managed step through existing collection commands.
