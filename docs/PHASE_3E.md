# Phase 3E: Opportunity Intelligence And Trader Cockpit UI

Phase 3E makes the local cockpit easier to read and adds payout-adjusted opportunity ranking. It keeps every action paper/demo-only.

## What It Adds

- Human-readable trade cards with short market names, category labels, recommendation, confidence, score, edge, price, spread, liquidity, time remaining, risk meter, and supporting signals.
- Traffic-light labels:
  - `Strong Opportunity`: high score, strong edge, fresh data, and acceptable spread.
  - `Watchlist`: enough score or edge to monitor.
  - `Avoid`: stale data, low score, high spread, low liquidity, or weak confidence.
- A Paper Portfolio header that answers how the paper book is doing now.
- Lightweight dashboard charts for paper P&L, forecast count, opportunity count, and model accuracy.
- `/opportunities/best-payouts`: payout-adjusted opportunities sorted by expected value, payout/risk ratio, and score.
- CLI commands:
  - `kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md`
  - `kalshi-bot ui-summary`

## Payout-Adjusted Score

The scanner now computes payout metrics for ranked opportunities:

- `payout_if_correct`
- `downside_if_wrong`
- `risk_adjusted_edge`
- `payout_to_risk_ratio`
- `expected_value`
- `expected_value_score`

Expected value is:

```text
expected_value = probability * payout_if_correct - (1 - probability) * downside_if_wrong
```

For BUY YES, the probability is the YES forecast. For BUY NO, the probability is `1 - YES forecast`.

The payout-adjusted score weights:

- expected value: 30%
- edge: 25%
- liquidity: 15%
- spread: 15%
- confidence: 10%
- freshness/time: 5%

Low-confidence longshots are filtered out of the best-payout page unless confidence and liquidity are acceptable.

## Reading The Dashboard

Start at `Today's Summary`. It shows markets scanned, forecasts generated, opportunities found, open paper trades, paper P&L, best model, best opportunity, and autopilot status.

Then read `Paper Portfolio` for current paper P&L, realized/unrealized P&L, open positions, open opportunities, and largest exposure.

Trade cards show:

- the short market name instead of the full raw title,
- a traffic-light label,
- BUY YES, BUY NO, or NO TRADE,
- a risk meter,
- the primary driver,
- supporting signals,
- risks,
- full market title behind `View Market Details`.

Technical/raw data remains hidden behind advanced details on the detail page.

## Commands

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot ui-summary
kalshi-bot ui
```

## Limits

Phase 3E does not add live trading, production execution, authenticated portfolio access, or private account data. Payout rankings, trade cards, traffic lights, and risk meters are local paper/demo review tools.
