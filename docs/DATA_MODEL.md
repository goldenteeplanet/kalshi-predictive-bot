# Data Model

The default database is SQLite at `data/kalshi_phase1.db`. JSON payloads are stored as text to keep the schema portable and auditable.

## `markets`

One row per market ticker.

- `ticker`: primary key.
- `event_ticker`, `series_ticker`: Kalshi hierarchy identifiers.
- `title`, `subtitle`, `market_type`, `status`, `result`: market descriptors and state.
- `open_time`, `close_time`, `expected_expiration_time`, `expiration_time`, `settlement_ts`: market lifecycle timestamps.
- `settlement_value_dollars`, `volume_fp`, `open_interest_fp`, `liquidity_dollars`: numeric fields stored as strings.
- `rules_primary`, `rules_secondary`: public rules text.
- `raw_json`: full market payload.
- `first_seen_at`, `last_seen_at`: local ingestion timestamps.

## `market_snapshots`

Append-only market observations.

- `id`: primary key.
- `ticker`: foreign key to `markets`.
- `captured_at`: local capture timestamp.
- `status`: market status at capture time.
- `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`: market quote fields when present.
- `best_yes_bid`, `best_yes_ask`, `best_no_bid`, `best_no_ask`: parsed best prices from public orderbook data or market quote fallback.
- `spread`: best YES ask minus best YES bid when available.
- `last_price_dollars`, `volume_fp`, `volume_24h_fp`, `open_interest_fp`: snapshot fields stored as strings.
- `raw_market_json`, `raw_orderbook_json`: full public payloads.

## `forecasts`

Append-only model outputs.

- `id`: primary key.
- `ticker`: foreign key to `markets`.
- `forecasted_at`: forecast timestamp, usually matching snapshot capture time.
- `model_name`: forecast model identifier.
- `yes_probability`: predicted YES probability stored as a string.
- `market_mid_probability`: midpoint probability when available.
- `best_yes_bid`, `best_yes_ask`: inputs used when available.
- `feature_json`: serialized features and source fields.
- `notes`: optional model notes.

## `settlements`

One row per settled ticker.

- `ticker`: primary key and foreign key to `markets`.
- `settled_at`: settlement timestamp when available.
- `result`: public settlement result.
- `yes_settlement_value`: normalized YES outcome value when available.
- `raw_json`: full settled market payload.
- `updated_at`: local update timestamp.

## `paper_orders`

Local simulated orders only. These rows are never sent to Kalshi.

- `id`: primary key.
- `ticker`: market ticker.
- `forecast_id`: optional link to the forecast that triggered the decision.
- `created_at`: local creation timestamp.
- `model_name`: forecast model used.
- `side`: `BUY_YES`, `BUY_NO`, `SELL_YES`, or `SELL_NO`.
- `probability`, `market_price`, `limit_price`, `edge`: decimal values stored as strings.
- `quantity`: simulated contract count.
- `status`: `OPEN`, `FILLED`, `CANCELLED`, or `EXPIRED`.
- `reason`: human-readable decision explanation.
- `raw_decision_json`: serialized strategy decision payload.

## `paper_fills`

Immediate simulated fills produced by Phase 2.

- `id`: primary key.
- `paper_order_id`: link to `paper_orders`.
- `ticker`, `filled_at`, `side`, `price`, `quantity`, `fee`: fill details.
- `raw_fill_json`: serialized simulated fill payload.

## `paper_positions`

Current paper position per ticker.

- `ticker`: primary key.
- `yes_contracts`, `no_contracts`: simulated long holdings.
- `avg_yes_price`, `avg_no_price`: weighted average entry prices.
- `realized_pnl`: settlement-based realized paper P&L.
- `updated_at`: local update timestamp.

## `paper_pnl`

Append-only paper P&L snapshots.

- `id`: primary key.
- `ticker`, `calculated_at`: market and calculation timestamp.
- `yes_contracts`, `no_contracts`, `avg_yes_price`, `avg_no_price`: position inputs.
- `settlement_result`: settlement state when available.
- `realized_pnl`, `unrealized_pnl`, `total_pnl`: decimal values stored as strings.
- `notes`: calculation context.

## `position_history`

Append-only workstation snapshots of current paper positions.

- `id`: primary key.
- `ticker`, `recorded_at`: position/time key.
- `position_size`: net YES minus NO paper contracts.
- `avg_cost`, `market_price`: local cost and latest market price context.
- `realized_pnl`, `unrealized_pnl`, `total_pnl`, `exposure`: decimal values stored as strings.

## `portfolio_snapshots`

Append-only workstation snapshots of the paper portfolio.

- `id`: primary key.
- `snapshot_time`: local snapshot timestamp.
- `total_positions`, `open_orders`: paper position and order counts.
- `total_exposure`, `realized_pnl`, `unrealized_pnl`, `total_pnl`: decimal values stored as strings.

## `watchlists`

Local workstation watchlist definitions.

- `id`: primary key.
- `name`: unique local watchlist name.
- `description`: optional review context.
- `created_at`: local creation timestamp.

## `watchlist_markets`

Ticker membership rows for local watchlists.

- `id`: primary key.
- `watchlist_id`: link to `watchlists`.
- `ticker`: market ticker to track.
- `added_at`: local add timestamp.
- `notes`: optional operator notes.

## `alerts`

Local workstation alert rules.

- `id`: primary key.
- `name`, `alert_type`: rule identity.
- `threshold`: rule threshold stored as a string when applicable.
- `enabled`: integer boolean flag.
- `created_at`, `raw_json`: audit context.

## `alert_events`

Append-only alert events created by local workstation evaluation.

- `id`: primary key.
- `alert_id`: optional link to `alerts`.
- `created_at`, `alert_type`, `ticker`, `severity`: event identity.
- `message`: human-readable alert text.
- `raw_json`: serialized source row or event context.
- `acknowledged_at`: optional local acknowledgement timestamp.

## `features`

Feature records for market-linked or global external data.

- `id`: primary key.
- `ticker`: market ticker or `*` for global features.
- `feature_set_name`: feature namespace such as `weather`, `crypto`, or `economic`.
- `generated_at`, `source_timestamp`, `created_at`: timestamps.
- `features_json`: normalized feature payload.
- `raw_source_json`: original source JSON when available.

## `feature_snapshots`

Combined feature snapshots built from market snapshots and latest external features.

- `ticker`, `captured_at`: market/time key.
- `market_features_json`: market-derived features.
- `external_features_json`: latest external feature payloads.
- `combined_features_json`: combined market and external features.

## `backtest_runs`

One row per historical backtest run.

- `name`, `strategy_name`, `model_name`: identifiers.
- `started_at`, `completed_at`, `start_time`, `end_time`: run timing.
- `config_json`: simulation settings.
- `summary_json`: aggregate metrics.
- `notes`: run context.

## `backtest_trades`

Simulated historical trades generated inside a backtest run.

- `backtest_run_id`: link to `backtest_runs`.
- `ticker`, `forecast_id`, `simulated_at`: trade source.
- `side`, `price`, `quantity`, `edge`: simulated decision.
- `settlement_result`, `pnl`: evaluated outcome.
- `raw_decision_json`: serialized decision details.

## `market_rankings`

Scored market rows from the opportunity scanner.

- Market metadata: `ticker`, `title`, `status`, `series_ticker`, `event_ticker`.
- Market quality fields: `volume`, `open_interest`, `liquidity`, `spread`, `midpoint`, `time_to_close_minutes`.
- Forecast fields: `forecast_model`, `forecast_probability`, `best_side`, `best_price`, `estimated_edge`.
- Component scores: `liquidity_score`, `spread_score`, `time_score`, `model_confidence_score`.
- `opportunity_score`, `reason`, `raw_json`: final ranking details.
- Phase 3E payout metrics such as `expected_value`, `payout_to_risk_ratio`, `risk_adjusted_edge`, and `payout_adjusted_score` are stored in `raw_json` for new ranking rows.

## `market_opportunities`

Subset of rankings that cleared configured edge and score thresholds.

- `ticker`, `detected_at`, `model_name`, `side`, `price`.
- `forecast_probability`, `estimated_edge`, `opportunity_score`.
- `status`, `reason`, `raw_json`.

## `overnight_runs`

One row per bounded overnight paper-learning run.

- `started_at`, `completed_at`, `status`: run lifecycle.
- `cycles_requested`, `cycles_completed`, `errors_count`: run counts.
- `config_json`: effective overnight settings.
- `summary_json`: run-level summary including paper orders, forecasts, opportunities, and errors.

## `overnight_cycles`

One row per overnight cycle.

- `overnight_run_id`, `cycle_number`: parent run and sequence.
- `started_at`, `completed_at`, `status`: cycle lifecycle.
- `markets_collected`, `snapshots_inserted`, `forecasts_inserted`: data capture counts.
- `paper_orders_created`, `opportunities_detected`, `settlements_synced`: paper-learning counts.
- `reports_generated`: generated report count.
- `errors_json`: per-step stored errors.
- `summary_json`: per-step cycle summary.

## `model_iteration_metrics`

Per-cycle learning feedback rows.

- `generated_at`, `cycle_number`, `model_name`: metric identity.
- `forecast_count`, `opportunity_count`, `paper_trade_count`: model activity counts.
- `estimated_pnl`, `realized_pnl`: latest paper P&L context.
- `avg_edge`, `avg_opportunity_score`: recent ranking averages.
- `notes`, `raw_json`: plain-English context and raw step details.

## `forum_consensus_signals`

Imported aggregate forum-consensus signals. These are not scraped automatically.

- `ticker`, `observed_at`, `source`, `side`: signal identity.
- `participant_count`, `winner_count`, `average_win_rate`: aggregate crowd quality fields.
- `longshot_price`, `consensus_score`: longshot and scoring context.
- `notes`, `raw_json`, `created_at`: audit fields.

## `model_leaderboard`

Model comparison snapshots.

- `model_name`, `generated_at`.
- Coverage: `forecast_count`, `evaluated_forecast_count`, `paper_trade_count`, `settled_trade_count`.
- Calibration: `brier_score`, `log_loss`.
- Paper/backtest performance: `win_rate`, `total_pnl`, `roi_on_exposure`, `avg_edge`, `max_drawdown`.
- `notes`, `raw_json`.

## `crypto_prices`

Append-only public crypto price observations.

- `id`: primary key.
- `symbol`, `source`, `observed_at`: asset/source/time key.
- `price_usd`: observed USD spot price stored as a string.
- `volume_24h`, `market_cap`: optional provider fields stored as strings.
- `raw_json`: original provider payload.
- `created_at`: local insert timestamp.

## `crypto_features`

Engineered crypto features derived from stored crypto prices.

- `id`: primary key.
- `symbol`, `source`, `generated_at`, `window_minutes`: feature identity.
- `price`: latest price used for feature generation.
- `return_5m`, `return_15m`, `return_1h`, `return_4h`, `return_24h`: return windows when enough history exists.
- `volatility_1h`, `volatility_4h`, `volatility_24h`: population volatility over return series when enough samples exist.
- `momentum_score`, `trend_direction`: normalized momentum signal used by `crypto_v2`.
- `raw_json`: full calculated feature payload.
- `created_at`: local insert timestamp.

## `crypto_market_links`

Detected links between stored Kalshi markets and crypto symbols.

- `id`: primary key.
- `ticker`: Kalshi market ticker.
- `symbol`: linked crypto symbol such as `BTC` or `ETH`.
- `detected_at`: link generation timestamp.
- `confidence`: decimal confidence score stored as a string.
- `reason`: human-readable match explanation.
- `raw_json`: detector details.

## `weather_observations`

Append-only weather observation rows, primarily for manual/offline data.

- `id`: primary key.
- `location_key`, `source`, `observed_at`: location/source/time key.
- `latitude`, `longitude`: optional coordinates stored as strings.
- `temperature_f`, `dewpoint_f`, `humidity`: observed atmospheric values.
- `wind_speed_mph`, `wind_gust_mph`, `precipitation_inches`: observed condition values.
- `raw_json`: original source payload.
- `created_at`: local insert timestamp.

## `weather_forecasts`

Append-only weather forecast periods from NOAA/NWS or manual JSON.

- `id`: primary key.
- `location_key`, `source`: forecast source identity.
- `forecast_generated_at`, `forecast_time`: model run time and target period time.
- `latitude`, `longitude`: optional coordinates stored as strings.
- `temperature_f`, `dewpoint_f`, `humidity`: forecast atmospheric values.
- `wind_speed_mph`, `wind_gust_mph`: forecast wind values.
- `precipitation_probability`, `precipitation_inches`: forecast precipitation values.
- `short_forecast`, `detailed_forecast`: provider text.
- `raw_json`: original period payload.
- `created_at`: local insert timestamp.

## `weather_features`

Engineered weather features derived from stored weather forecasts.

- `id`: primary key.
- `location_key`, `source`, `generated_at`, `target_time`: feature identity.
- `temperature_f`, `precipitation_probability`, `expected_precipitation_inches`: target weather inputs.
- `wind_speed_mph`, `wind_gust_mph`, `heat_index_f`: target wind/heat values.
- `freeze_risk_score`, `rain_risk_score`, `wind_risk_score`: normalized risk features.
- `temp_anomaly_score`: seasonal deviation score when baseline data exists.
- `weather_confidence_score`: freshness and lead-time confidence feature.
- `raw_json`: full calculated feature payload and notes.
- `created_at`: local insert timestamp.

## `weather_market_links`

Detected links between stored Kalshi markets and weather features.

- `id`: primary key.
- `ticker`: Kalshi market ticker.
- `location_key`: detected location key or `unknown`.
- `detected_at`: link generation timestamp.
- `weather_metric`: `TEMPERATURE`, `RAIN`, `SNOW`, `WIND`, `HURRICANE`, `FREEZE`, or `UNKNOWN`.
- `target_operator`: `ABOVE`, `BELOW`, `AT_OR_ABOVE`, `AT_OR_BELOW`, `EQUALS`, or `UNKNOWN`.
- `target_value`: detected numeric threshold when available.
- `target_time`: target time inferred from market close/expiration fields when available.
- `confidence`: decimal confidence score stored as a string.
- `reason`: human-readable match explanation.
- `raw_json`: detector details.

## `model_tournament_runs`

One row per model tournament execution.

- `id`: primary key.
- `name`, `started_at`, `completed_at`, `days`: run identity and lookback window.
- `config_json`: tournament configuration, including model list and weight generation flag.
- `summary_json`: aggregate row/diagnostic/weight counts.
- `notes`: run context.

## `model_tournament_results`

Per-model, per-category tournament result rows.

- `tournament_run_id`: link to `model_tournament_runs`.
- `model_name`, `category`: model and detected market category.
- Forecast coverage: `forecast_count`, `evaluated_forecast_count`.
- Simulated activity: `simulated_trade_count`, `settled_trade_count`.
- Calibration: `brier_score`, `log_loss`.
- Performance: `win_rate`, `total_pnl`, `roi_on_exposure`, `avg_edge`, `max_drawdown`.
- Rankings: `calibration_rank`, `pnl_rank`, `overall_rank`.
- `status`: `OK` or `INSUFFICIENT_DATA`.
- `notes`, `raw_json`.

## `model_weights`

Generated category-specific model weights.

- `generated_at`, `model_name`, `category`.
- `weight`: normalized decimal weight stored as a string.
- `method`: weighting method, such as `tournament_v1` or `fallback_market_implied`.
- `lookback_days`: tournament window used to generate the weight.
- `raw_json`: weighting notes and source details.

## `model_diagnostics`

Diagnostics produced from tournament rows.

- `generated_at`, `model_name`, `category`.
- `diagnostic_type`: calibration, P&L, sample size, category coverage, skipped forecasts, or overconfidence.
- `metric_name`, `metric_value`: measured diagnostic input.
- `notes`: human-readable interpretation.
- `raw_json`: diagnostic source context.
