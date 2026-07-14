# Scheduler Profiles

Scheduler profiles are local advisory plans. They list commands and cadence, but they do not start
production execution or place real orders.

## Sports Watch

Inspect the profile:

```bash
kalshi-bot scheduler-plan --profile sports-watch
```

The `sports-watch` profile is intended for a 10-minute paper/demo sports loop:

1. Import the latest manual/public-free sports data file when available.
2. Link sports markets to stored games.
3. Build sports features and sports signals.
4. Forecast linked sports markets with `sports_v1`.
5. Write the sports opportunity report for review.

The profile remains paper/demo only. If the import file is not present, skip that import step and
continue with linking/features against the currently stored local sports data.

## Microstructure Watch

Inspect the profile:

```bash
kalshi-bot scheduler-plan --profile microstructure-watch
```

The `microstructure-watch` profile is intended for a 5-minute paper/demo market-quality loop:

1. Collect open public market snapshots and orderbooks.
2. Build microstructure features from stored snapshots.
3. Forecast markets with `microstructure_v1`.
4. Find paper/demo opportunities for `microstructure_v1`.
5. Refresh the Signal Marketplace report.
6. Write the microstructure report.

The profile remains paper/demo only. The feature builder uses stored data and does not place trades.

## Meta Watch

Inspect the profile:

```bash
kalshi-bot scheduler-plan --profile meta-watch
```

The `meta-watch` profile is intended for a 15-minute paper/demo model-selection loop:

1. Build meta features from local market, model, signal, and data-quality state.
2. Refresh settled-market meta training examples.
3. Forecast with `meta_model_v1`.
4. Forecast with `meta_ensemble_v1`.
5. Compare meta results against `ensemble_v2` and `market_implied_v1`.
6. Write the meta model report.
7. Refresh the Signal Marketplace report.

The profile remains paper/demo only. It chooses which local model to trust and does not place trades.
