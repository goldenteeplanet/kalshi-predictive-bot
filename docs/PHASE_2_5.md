# Phase 2.5

## Objective

Add a feature store, external data ingestion scaffolding, multi-model forecasting, historical backtesting, and strategy comparison while keeping the system read-only and simulated.

## What Changed

- New `features` and `feature_snapshots` tables.
- Manual external JSON ingestion for weather, crypto, and economic data.
- Forecast model registry with `market_implied_v1`, `weather_v1`, `crypto_v1`, `economic_v1`, and `ensemble_v1`.
- Historical backtesting over stored forecasts, snapshots, and settlements.
- Strategy comparison report across available models.

## Feature Store

Market feature snapshots include best bids/asks, spread, midpoint, volume, open interest, liquidity, time to close, status, and series/event/title fields. External features are stored as JSON and combined with market features for model use.

## External Ingestion

External ingestion is scaffolding only. It supports manually supplied JSON and does not require paid APIs or credentials.

```bash
kalshi-bot ingest-external --source weather --input-file path.json
kalshi-bot ingest-external --source crypto --input-file path.json
kalshi-bot ingest-external --source economic --input-file path.json
```

## Model Registry

Forecasts can run by model:

```bash
kalshi-bot forecast --model market_implied_v1
kalshi-bot forecast --model weather_v1
kalshi-bot forecast --model crypto_v1
kalshi-bot forecast --model economic_v1
kalshi-bot forecast --model ensemble_v1
kalshi-bot forecast --model all
```

External models only forecast when relevant stored features contain an explicit usable probability. They skip cleanly rather than invent predictions.

## Backtesting

Backtests use local forecasts, snapshots, and settlements only. They simulate the Phase 2 paper strategy historically and evaluate only settled markets.

```bash
kalshi-bot backtest --model-name market_implied_v1 --strategy paper_v1 --days 30 --output reports/backtest_market_implied_v1.md
```

## Strategy Comparison

Strategy comparison runs the same historical paper simulation for the configured model list and reports forecast counts, evaluated forecasts, simulated trades, win rate, P&L, ROI, Brier score, and log loss.

```bash
kalshi-bot compare-strategies --days 30 --output reports/strategy_comparison.md
```

## Limitations

- No live trading.
- No Kalshi authentication.
- No real order placement.
- No live external API integrations.
- External models require explicit probability-bearing JSON.
- Backtests use simplified immediate-fill assumptions.

## Before Phase 3

Phase 3 should not begin until:

- At least 100 settled simulated trades.
- Positive P&L after conservative assumptions.
- Positive calibration compared to `market_implied_v1`.
- No duplicate order bugs.
- Risk limits verified.
- Manual review of top wins and losses.

