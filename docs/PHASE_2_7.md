# Phase 2.7 Crypto Forecasting

Phase 2.7 adds real public crypto price ingestion and a crypto-specific forecast model while preserving the project safety boundary: no live trading, no Kalshi authentication, and no real order placement.

## Scope

- Store public crypto prices from no-key endpoints.
- Build return, volatility, trend, and momentum features from stored crypto prices.
- Detect crypto-linked Kalshi markets from stored market metadata.
- Run `crypto_v2` forecasts from stored snapshots, links, and features.
- Generate crypto feature and crypto backtest Markdown reports.
- Include `crypto_v2` in normal opportunity scans and leaderboard output.

## Commands

```bash
kalshi-bot ingest-crypto --symbols BTC,ETH --source coinbase
kalshi-bot build-crypto-features --symbols BTC,ETH
kalshi-bot link-crypto-markets
kalshi-bot forecast --model crypto_v2
kalshi-bot find-opportunities --model-name crypto_v2 --limit 20 --output reports/opportunities_crypto_v2.md
kalshi-bot crypto-report --symbols BTC,ETH --output reports/crypto_features.md
kalshi-bot crypto-backtest --days 30 --output reports/crypto_backtest.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

## Settings

| Variable | Default | Purpose |
|---|---:|---|
| `CRYPTO_V2_MAX_ADJUSTMENT` | `0.08` | Maximum probability shift away from the market midpoint. |
| `CRYPTO_V2_MIN_LINK_CONFIDENCE` | `0.6` | Minimum market-link confidence required for forecasts. |
| `CRYPTO_V2_MIN_HISTORY_MINUTES` | `60` | Minimum price history expected before momentum features are useful. |

## Model Behavior

`crypto_v2` starts from the stored market midpoint, reads the latest crypto market link and latest crypto features for that symbol, then applies a bounded momentum adjustment. Positive momentum increases YES probability for markets phrased as above/over/exceed and decreases YES probability for markets phrased as below/under. Ambiguous market direction gets no momentum adjustment.

The model skips cleanly when market links, feature rows, momentum, or usable market prices are missing.
