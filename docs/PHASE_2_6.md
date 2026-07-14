# Phase 2.6

## Objective

Add an opportunity scanner, market ranking engine, and model leaderboard while keeping the system read-only and simulated.

## Opportunity Scoring

The scanner scores markets from latest stored snapshots and latest stored forecasts. It does not call live trading, order, portfolio, balance, or authenticated endpoints.

Scores are 0 to 100 and use these weights:

- Edge: 35%
- Liquidity: 20%
- Spread: 20%
- Time to close: 10%
- Model confidence: 15%

Lower spreads score better. Higher volume, open interest, and liquidity score better. Near-expiration markets are penalized, and very long-dated markets are lower priority. Missing data is scored conservatively rather than crashing.

## Market Ranking

`find-opportunities` inserts recent `market_rankings` rows for top scored markets. It inserts `market_opportunities` only when configured edge and score thresholds are met.

## Model Leaderboard

The leaderboard compares `market_implied_v1`, `weather_v1`, `crypto_v1`, `economic_v1`, and `ensemble_v1`. Models with no data are included with clear notes instead of crashing.

## How To Run

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name market_implied_v1 --limit 20 --output reports/opportunities.md
kalshi-bot market-rankings --limit 50 --output reports/market_rankings.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

## How To Interpret Reports

- `opportunities.md`: markets that cleared configured edge and score thresholds.
- `market_rankings.md`: recent scored markets whether or not they qualified as opportunities.
- `model_leaderboard.md`: model coverage, calibration, paper/backtest performance, and insufficient-data notes.

## Limitations

- No live trading.
- No Kalshi authentication.
- No real order placement.
- Opportunity scores are heuristics, not trading signals.
- Leaderboard quality depends on settled forecasts and simulated trades.
- Missing data is intentionally conservative.

## Why This Still Should Not Trade Real Money

The scanner and leaderboard are diagnostic tools. They do not model all execution risk, liquidity dynamics, adverse selection, fees, slippage, or data delays. Any future real-money phase would require separate risk controls, manual review, credential isolation, and explicit approval.

