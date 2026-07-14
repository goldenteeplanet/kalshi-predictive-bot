# Phase 2

## Objective

Add a paper trading ledger on top of the Phase 1 read-only data and forecasting foundation.

## What Paper Trading Means

Paper trading creates local simulated orders, simulated fills, positions, and P&L rows. These records never leave the local database and are never sent to Kalshi.

Phase 2 does not add authentication, signing, private endpoints, live orders, balances, positions, portfolio reads, or execution engines.

## Strategy Logic

For the latest forecast per ticker:

- Read `yes_probability`.
- Use `best_yes_ask` as the simulated BUY_YES executable price.
- Use `best_no_ask` as the simulated BUY_NO executable price.
- BUY_YES edge is `yes_probability - best_yes_ask`.
- BUY_NO edge is `(1 - yes_probability) - best_no_ask`.
- Create the best qualifying decision when edge is at least `PAPER_MIN_EDGE`.
- Skip duplicate orders for the same forecast.
- Prefer one latest forecast per ticker.

## Risk Controls

- `PAPER_MIN_EDGE`: minimum required edge.
- `PAPER_MAX_ORDER_QUANTITY`: simulated quantity per order.
- `PAPER_MAX_POSITION_PER_MARKET`: position cap for YES and NO contracts.
- `PAPER_MAX_OPEN_ORDERS`: cap on outstanding paper orders.
- `PAPER_ALLOW_BUY_NO`: enables or disables BUY_NO decisions.
- `PAPER_ALLOW_SELLING`: defaults to false; Phase 2 does not rely on selling.
- `PAPER_ORDER_TTL_MINUTES`: reserved for open-order expiry workflows.

## How Fills Are Simulated

Phase 2 uses immediate fills:

- BUY_YES fills immediately at the stored limit/market price.
- BUY_NO fills immediately at the stored limit/market price.
- Fees are `PAPER_DEFAULT_FEE_PER_CONTRACT * quantity`.
- Fills update local paper positions and weighted average prices.
- Realized P&L stays at zero until settlement.

## Limitations

- Immediate fills are optimistic.
- No order book depth, queue position, partial fill, or slippage model is included.
- Paper positions are long-only for Phase 2.
- P&L is only as current as the latest stored snapshots and settlements.
- Reports are diagnostic, not trading advice.

## How To Run

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot paper-run
kalshi-bot paper-summary --output reports/paper_trading.md
kalshi-bot paper-pnl
```

To reset only paper trading data:

```bash
kalshi-bot paper-reset --yes
```

## How To Interpret Results

- `paper-run` shows how many latest forecasts were scanned, how many decisions cleared edge, and how many simulated fills were created.
- `paper-summary` shows orders, positions, estimated unrealized P&L, and top exposures.
- `paper-pnl` writes append-only P&L rows using latest snapshots for open markets and settlement outcomes for settled markets.

Review duplicate skips, risk-limit skips, and top losing markets before trusting any strategy conclusions.

## When Phase 3 Should Begin

Phase 3 should only begin after:

- At least 100 settled paper trades.
- Positive Brier/calibration trend.
- Positive simulated P&L after conservative fees.
- No major duplicate-order or position-limit bugs.
- Manual review of top losing markets.

