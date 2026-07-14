# Phase 3L: Meta Model

Phase 3L adds a local Meta Model that predicts which forecasting model to trust for each
market, category, time horizon, and data condition. It is not a new live-trading path and
does not place real orders.

## Concept

`meta_model_v1` selects the most trustworthy available model for a market using local
features, stored forecasts, model performance, signal activity, data freshness, liquidity,
spread quality, and model disagreement.

`meta_ensemble_v1` blends available model probabilities using the same trust scores. When
one model clearly dominates, it gives that model most of the weight. When models disagree,
it leans more cautiously toward the market-implied baseline.

## Local Workflow

```bash
kalshi-bot build-meta-features --model-scope all
kalshi-bot build-meta-training --days 90
kalshi-bot forecast --model meta_model_v1
kalshi-bot forecast --model meta_ensemble_v1
kalshi-bot meta-evaluate --days 90 --output reports/meta_evaluation.md
kalshi-bot meta-report --output reports/meta_report.md
kalshi-bot meta-opportunities --limit 20 --output reports/meta_opportunities.md
kalshi-bot scheduler-plan --profile meta-watch
kalshi-bot ui
```

## Trust Logic

The deterministic selector scores candidate models from 0 to 100 using:

- category fit and feature availability
- current model probability availability
- recent calibration and ROI evidence
- category-specific leaderboard evidence
- signal support
- data freshness
- model agreement or disagreement
- liquidity and spread penalties
- fallback penalties when evidence is thin

If no candidate qualifies, the selector falls back to `ensemble_v2`. If `ensemble_v2` is
not available, it falls back to `market_implied_v1`.

## Data Stored

Phase 3L persists:

- `meta_model_features`
- `meta_model_decisions`
- `meta_model_training_examples`
- `meta_model_performance`

These tables are local diagnostic records only.

## UI And Reports

The UI adds `/meta` and `/meta/{ticker}`. Opportunity cards show the selected trusted
model, trust score, and fallback badge when fallback logic was used.

Reports include:

- `reports/meta_report.md`
- `reports/meta_evaluation.md`
- `reports/meta_opportunities.md`

The Research Assistant adds a "Why this model was selected" explanation when a meta
decision exists.

## Limitations

The first implementation is deterministic and intentionally conservative. It needs settled
history before performance comparisons are meaningful, and early decisions may fall back to
`ensemble_v2` or `market_implied_v1`. All outputs remain read-only, paper-only, or demo-only.
