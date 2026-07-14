# Phase 2.9 Model Tournament And Ensemble v2

Phase 2.9 adds a model tournament layer that compares local models, generates diagnostics, creates category-specific weights, and uses those weights in `ensemble_v2`.

## Objective

- Compare `market_implied_v1`, `crypto_v2`, `weather_v2`, `economic_v1`, `ensemble_v1`, and `ensemble_v2`.
- Group results by category when possible: crypto, weather, economic, sports, general, or unknown.
- Persist tournament runs, result rows, diagnostics, and model weights.
- Generate tournament, diagnostics, and weights reports.
- Improve ensemble weighting without adding live trading or authenticated Kalshi access.

## Tournament Concept

The tournament reads only local data:

- stored forecasts
- public settlements already synced locally
- paper orders
- backtest trades

Models with too little settled data remain visible and are marked `INSUFFICIENT_DATA`. This avoids hiding weak evidence behind an empty leaderboard.

## Ranking Logic

Ranks are assigned per category.

- Calibration rank favors lower Brier score and log loss.
- P&L rank favors higher simulated ROI and total P&L.
- Overall rank uses a conservative blend: calibration 40%, ROI/P&L 35%, sample size 15%, drawdown penalty 10%.

This is a diagnostic ranking, not an execution signal.

## Diagnostics

Diagnostics flag:

- insufficient settled forecasts
- negative simulated P&L or ROI
- weak calibration and possible overconfidence
- high drawdown relative to total P&L
- category coverage limits
- skipped forecasts that lack settlements

The diagnostics report includes a recommended action for each row.

## Model Weights

Weights are generated per category.

- Insufficient-data models receive weight `0`.
- Poor calibration and negative ROI reduce scores.
- Larger evaluated samples increase confidence.
- Weights normalize to `1.0` per category.
- If no model has enough data, `market_implied_v1` gets fallback weight `1.0`.

## Ensemble v2

`ensemble_v2` combines stored component forecasts:

- `market_implied_v1`
- `crypto_v2`
- `weather_v2`
- `economic_v1`

It uses category weights from `model_weights`. If no usable weights exist, it falls back to a simple average of available stored component forecasts. If no component forecasts exist, it skips.

## How To Run

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot tournament --days 30 --output reports/model_tournament.md
kalshi-bot model-diagnostics --days 30 --output reports/model_diagnostics.md
kalshi-bot model-weights --days 30 --output reports/model_weights.md
kalshi-bot forecast --model ensemble_v2
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

## Interpreting Results

- Treat `INSUFFICIENT_DATA` as a blocker, not a model verdict.
- Prefer models that beat `market_implied_v1` on both calibration and simulated ROI.
- Review drawdown before trusting P&L.
- Use diagnostics to decide whether the next step is more data, feature work, or model tuning.

## Phase 3 Requirements

Phase 3 remains blocked until:

- 100+ settled paper trades exist.
- At least one model beats `market_implied_v1` on calibration.
- At least one model beats `market_implied_v1` on simulated ROI.
- `ensemble_v2` is not worse than `market_implied_v1`.
- Drawdown is acceptable.
- Manual review of top wins/losses is complete.

## Safety

Phase 2.9 is still paper/simulation only. It adds no live trading, no Kalshi authentication, no account access, and no real order placement.
