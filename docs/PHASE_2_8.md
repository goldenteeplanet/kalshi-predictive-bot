# Phase 2.8 Weather Forecasting

Phase 2.8 adds public weather forecasting support while preserving the project safety boundary: no live trading, no Kalshi authentication, and no real order placement.

## Objective

- Ingest public weather forecast data from NOAA/NWS.
- Store weather observations, forecasts, engineered weather features, and market links.
- Detect weather-related Kalshi markets from stored public market text.
- Run `weather_v2` forecasts from stored snapshots, links, and features.
- Generate weather feature and weather backtest reports.
- Include `weather_v2` in opportunity scans and leaderboard output.

## NOAA/NWS Ingestion

`ingest-weather` uses the public `api.weather.gov` flow:

1. Fetch `/points/{lat},{lon}`.
2. Read `forecastHourly` or `forecast` from the returned properties.
3. Fetch forecast periods.
4. Store parsed hourly periods in `weather_forecasts`.

The provider uses the configured `KALSHI_USER_AGENT`, requires no API key, and reports network or parser errors without crashing the command.

Manual/offline ingestion is also supported:

```bash
kalshi-bot ingest-weather --location-key kansas_city --input-file data/weather_sample.json
```

## Feature Engineering

`build-weather-features` uses stored `weather_forecasts` only. It does not call live APIs.

Features include:

- `freeze_risk_score`
- `rain_risk_score`
- `wind_risk_score`
- `temp_anomaly_score`
- `weather_confidence_score`

Raw feature JSON includes calculation notes, forecast age, lead time, and source target time.

## Market Linking

`link-weather-markets` scans stored Kalshi market text and detects likely weather metric, operator, location, threshold, and target time.

Supported metrics include temperature, rain, snow, wind, hurricane, and freeze. Known location matches include Kansas City, New York, Los Angeles, Chicago, Miami, Dallas, Seattle, Denver, and Boston. Ambiguous weather markets can still link with lower confidence and `unknown` location.

## Model Logic

`weather_v2` starts from the stored market-implied midpoint and applies a small bounded adjustment:

- Temperature markets compare forecast temperature to the detected threshold.
- Rain markets use rain risk and precipitation probability.
- Wind markets use wind risk.
- Freeze markets use freeze risk.
- Probability is clamped between `0.01` and `0.99`.

The adjustment is bounded by `WEATHER_V2_MAX_ADJUSTMENT`, default `0.10`.

## Limitations

- NOAA outages or throttling can leave no fresh forecast rows.
- Market linking is text-based and may misclassify ambiguous titles.
- Seasonal temperature anomaly is null until a baseline source exists.
- Backtests require local forecasts, linked markets, model forecasts, and settlements.
- All outputs remain paper/simulation only.

## Commands

```bash
kalshi-bot ingest-weather --location-key kansas_city --lat 39.0997 --lon -94.5786
kalshi-bot build-weather-features --location-key kansas_city
kalshi-bot link-weather-markets
kalshi-bot forecast --model weather_v2
kalshi-bot find-opportunities --model-name weather_v2 --limit 20 --output reports/opportunities_weather_v2.md
kalshi-bot weather-report --location-key kansas_city --output reports/weather_features.md
kalshi-bot weather-backtest --days 30 --output reports/weather_backtest.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

These commands ingest public data and generate local diagnostics only. They do not place or route orders.
