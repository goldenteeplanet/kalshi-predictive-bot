# Roadmap

## Phase 1: Read-Only Data + Baseline Forecasting

- Public market data ingestion.
- Public orderbook snapshots.
- Local persistence.
- Market-implied baseline forecasts.
- Calibration/evaluation reports.

## Phase 2: Paper Trading Ledger

- Simulated orders and fills.
- Strategy P&L accounting.
- Slippage and fill assumptions.
- No live execution.
- Position limits and duplicate-order prevention.
- Paper trading report generation.

## Phase 3: Demo Execution

- Controlled demo-only execution workflows.
- Strict dry-run defaults.
- Operator confirmations.
- Expanded audit logs.
- Local demo autopilot with run/cycle/risk-event logs.
- Guardrails for stale data, limits, drawdown, duplicate attempts, and kill switch.
- Human-readable decision cockpit and explainability layer.

Phase 3 should only begin after at least 100 settled paper trades, at least one model beats `market_implied_v1` on calibration, at least one model beats `market_implied_v1` on simulated ROI, `ensemble_v2` is not worse than `market_implied_v1`, drawdown is acceptable, no major duplicate-order or position-limit bugs exist, and manual review of top wins/losses is complete.

## Phase 2.5: Backtesting And Model Comparison

- Feature store for market and external features.
- Manual external JSON ingestion scaffolding.
- Multiple forecast model registry.
- Ensemble model.
- Historical backtesting over stored data only.
- Strategy comparison reports.

Phase 3 should not begin until at least 100 settled simulated trades exist, P&L is positive after conservative assumptions, calibration improves against `market_implied_v1`, duplicate-order bugs are absent, risk limits are verified, and top wins/losses have been manually reviewed.

## Phase 2.6: Opportunity Scanner And Leaderboard

- Rank markets by edge, liquidity, spread, time-to-close, and model confidence.
- Store market ranking and opportunity rows.
- Generate opportunity and ranking Markdown reports.
- Build a model leaderboard across calibration, paper/backtest activity, and P&L.
- Keep all outputs diagnostic and simulated.

## Phase 2.7: Crypto Forecasting

- Ingest public no-key crypto prices for configured symbols.
- Store crypto prices, engineered crypto features, and crypto-to-Kalshi market links.
- Add `crypto_v2`, a bounded momentum-adjusted crypto forecaster.
- Generate crypto feature and crypto backtest reports.
- Include `crypto_v2` in opportunity scans and model leaderboard comparisons.
- Keep all Kalshi behavior read-only and all trading outputs simulated.

## Phase 2.8: Weather Forecasting

- Ingest public NOAA/NWS weather forecasts for configured locations.
- Store weather observations, forecasts, engineered features, and weather-market links.
- Add `weather_v2`, a bounded weather-adjusted forecaster.
- Generate weather feature and weather backtest reports.
- Include `weather_v2` in opportunity scans and model leaderboard comparisons.
- Keep all Kalshi behavior read-only and all trading outputs simulated.

## Phase 2.9: Model Tournament And Ensemble v2

- Compare supported models across calibration, simulated P&L, sample size, and drawdown.
- Store tournament runs, result rows, diagnostics, and category-specific weights.
- Add `ensemble_v2`, a stored-weight ensemble over component forecasts.
- Generate tournament, diagnostics, and model weights reports.
- Include tournament rank and category winner context in the leaderboard.
- Keep all Kalshi behavior read-only and all trading outputs simulated.

## Phase 3B: Demo Autopilot And Guardrails

- Run local scheduled demo-only cycles.
- Persist `autopilot_runs`, `autopilot_cycles`, and `risk_events`.
- Keep `AUTOPILOT_ENABLED=false` and `AUTOPILOT_DRY_RUN=true` as defaults.
- Block non-demo environments, stale data, low edge, low score, daily limits, duplicate ticker/model/side attempts, drawdown breaches, unsupported models, and kill switch activation.
- Expose status, one-cycle, scheduler, and report CLI commands.
- Add a local UI page with a dry-run-only cycle button and report link.
- Do not add production live trading or real-money order routing.

## Phase 3C: Human-Readable Decision UI And Explainability

- Redesign the local UI around plain-English opportunity cards.
- Explain why a market is interesting, why it is risky, what the bot would do, and what action is recommended.
- Add Forecast, Market Quality, Risk Checks, Paper History, Backtest History, and Raw Data tabs on the detail page.
- Hide raw JSON by default under Advanced / Raw Data.
- Make the autopilot page explain off, dry-run, blocked, and last-cycle states.
- Add report cards with last-generated timestamps when files exist.
- Add `explain-opportunity` CLI output for operators.
- Keep all behavior demo-only and paper-only.

## Phase 3C.5: Overnight Paper Learning And Consensus Signals

- Run a bounded overnight scheduler for data capture and paper-learning cycles.
- Persist `overnight_runs`, `overnight_cycles`, and `model_iteration_metrics`.
- Collect public market data, run all forecast models, refresh model weights when enough data exists, forecast `ensemble_v2`, scan opportunities, create paper bets, update paper P&L, sync settlements, and generate reports.
- Keep `OVERNIGHT_ENABLED=false`, `OVERNIGHT_RUN_PAPER=true`, and `OVERNIGHT_RUN_DEMO=false` as defaults.
- Continue after step errors unless `OVERNIGHT_STOP_ON_ERROR=true`.
- Add `/overnight` UI status, cycle history, model metrics, health, and recommended next action.
- Import aggregate forum-consensus longshot signals and expose them in opportunity explanations when enough historically winning participants support a longshot.
- Keep all behavior paper-only and demo-only; do not add production live trading.

## Phase 3D: Trader Workstation And Position Management

- Persist `position_history`, `portfolio_snapshots`, `watchlists`, `watchlist_markets`, `alerts`, and `alert_events`.
- Add portfolio, position detail, model performance, market monitor, analytics, watchlist, and alert UI pages.
- Convert the local dashboard into a workstation layout with left navigation, center review surface, and right intelligence rail.
- Add `portfolio-summary`, `daily-briefing`, and `analytics-report` CLI commands.
- Keep all position and alert management local to paper/demo data; do not add live account access or production order management.

## Phase 3E: Opportunity Intelligence And Trader Cockpit UI

- Replace raw title-heavy opportunity cards with short, human-readable trade cards.
- Add traffic-light labels, risk meters, category labels, primary drivers, supporting signals, and collapsed market details.
- Add payout-adjusted scoring from expected value, edge, liquidity, spread, confidence, and time/freshness.
- Add `/opportunities/best-payouts`, `best-payouts`, `ui-summary`, and `reports/best_payouts.md`.
- Keep low-confidence longshots out of best-payout rankings unless confidence and liquidity are acceptable.
- Keep all cockpit output paper-only and demo-only; do not add live execution.

## Phase 3F: AI Research Assistant

- Add deterministic local research evidence from rankings, forecasts, snapshots, features, model performance, paper data, fills, backtests, and settlements.
- Add `research_notes`, `research_questions`, and `opportunity_research_snapshots`.
- Add `research-opportunity`, `ask-research`, `research-report`, and `reports/research_report.md`.
- Add `/research`, `/research/opportunity/{ticker}`, `POST /research/ask`, and `Why?` links on opportunity cards.
- Store research snapshots for simple score, edge, rank, and recommendation comparisons across runs.
- Keep the assistant evidence-based and local; do not call external LLM APIs or enable live execution.

## Phase 3F-1: Learning Mode And Model Confidence Engine

- Add `LEARNING_MODE=true` defaults with lower paper-only thresholds for primary paper-trade generation.
- Persist `learning_runs`, `learning_cycles`, `learning_trade_targets`, and `model_confidence_scores`.
- Prioritize fast-settling paper targets so mistakes become measurable sooner.
- Score models by settled forecast calibration, settled paper P&L, sample size, and drawdown.
- Label models as Leader, Promising, Needs More Data, or Underperforming.
- Feed dynamic confidence weights into `ensemble_v2` through stored `model_weights`.
- Add `/learning`, `/models/confidence`, learning reports, model-confidence reports, and learning target reports.
- Keep Learning Mode paper-only; do not add demo or live trading.

## Phase 3G: Signal Marketplace

- Add extensible signal registry with Weather, Crypto, Economic, Market Divergence, Liquidity, Spread Compression, Momentum, Ensemble Agreement, Opportunity Score, and Fresh Data signals.
- Persist `signals`, `signal_events`, `signal_forecasts`, `signal_trades`, and `signal_performance`.
- Attribute active signals to forecasts and paper orders, then calculate ROI, win rate, P&L, calibration, confidence, and sample-size status by signal.
- Add `/signals`, `/signals/{signal_name}`, signal badges on opportunity cards, and signal-aware Research Assistant drivers.
- Add `signal-explorer`, `signal-leaderboard`, `signal-performance`, `signal-report`, and `reports/signal_report.md`.
- Keep all signal rankings diagnostic and paper/demo only; do not add live execution.

## Database Hardening Layer

- Support SQLite by default and PostgreSQL through `DB_BACKEND`/`DATABASE_URL`.
- Enable SQLite WAL, busy timeout, and normal synchronous mode.
- Add Alembic migration scaffolding and DB health, doctor, backup, recovery, and SQLite-to-Postgres copy commands.
- Surface database status on the dashboard and `/settings/database`.
- Warn on SQLite inside OneDrive and optionally require PostgreSQL for overnight runs.
- Keep all trading behavior paper/demo only; do not add live execution.

## Phase 3H: News Intelligence

- Ingest public RSS feeds and manual JSON/CSV news into local `news_items`.
- Classify news by market-relevant category, sentiment, importance, freshness, and entities.
- Link stored news to stored markets, build `news_features`, and generate News, Breaking News, Economic News, Crypto News, Weather News, and Sports News signals.
- Add `news_v1`, a bounded midpoint-adjusted model that only forecasts markets with news features.
- Add `/news`, `/news/{id}`, news reports, news opportunities, and news backtests.
- Keep all outputs local, deterministic, paper/demo only, and free of external LLM or paid API requirements.

## Phase 3J: Sports Intelligence

- Ingest manual JSON/CSV sports data for MLB, NBA, NFL, and NHL into local sports tables.
- Add public/free provider scaffolding for schedules, standings, scores, injuries, odds, and weather, with manual import as the first supported path.
- Classify sports markets by league and type, then link markets to games with confidence scores.
- Build bounded sports features from team strength, injuries, rest, travel, odds, and weather context.
- Add `mlb_v1`, `nba_v1`, `nfl_v1`, `nhl_v1`, and `sports_v1`; each skips unlinked markets or missing features.
- Generate sports signals and attribute them through the Signal Marketplace.
- Add `/sports`, `/sports/leagues/{league}`, `/sports/games/{game_key}`, sports reports, sports opportunities, sports backtests, and a `sports-watch` scheduler plan.
- Keep all outputs public/free, read-only, paper/demo only, and free of live-trading or paid-data requirements.

## Phase 3K: Market Microstructure Engine

- Analyze stored market snapshots and stored orderbook JSON without live feature-builder calls.
- Persist `microstructure_features`, `microstructure_events`, `microstructure_signals`, and `orderbook_depth_snapshots`.
- Detect spread tightening/widening, liquidity improvement/drying, orderbook pressure, pressure flips, price dislocations, cross-model disagreement, late moves, and possible informed flow.
- Add microstructure signals to the Signal Marketplace and Research Assistant context.
- Add `microstructure_v1`, a bounded midpoint-adjusted forecast model that skips markets without enough microstructure data.
- Add `/microstructure`, `/microstructure/{ticker}`, microstructure opportunity-card context, reports, backtests, and a `microstructure-watch` scheduler plan.
- Treat possible informed flow as a cautious heuristic, not proof of smart money.
- Keep all outputs read-only, paper/demo only, and free of live-trading behavior.

## Phase 3L: Meta Model

- Persist meta feature, decision, training-example, and performance tables.
- Build meta features from markets, stored forecasts, signal activity, model performance, data freshness, feature availability, spread, liquidity, and model disagreement.
- Add a deterministic selector that predicts which existing model should be trusted for each market.
- Add `meta_model_v1` and optional weighted `meta_ensemble_v1`.
- Compare meta performance against `ensemble_v2` and `market_implied_v1`.
- Add Meta Model UI pages, opportunity-card trust badges, Research Assistant explanations, meta reports, meta signals, and a `meta-watch` scheduler profile.
- Keep all outputs read-only, paper/demo only, and free of live-trading behavior.

## Phase 3O: Market Memory

- Persist `market_memory`, `forecast_memory`, and `trade_memory` as durable point-in-time learning stores.
- Capture market snapshots, forecasts, failures, opportunities, Phase 3M sizing decisions, Phase 3N risk decisions, paper trade lifecycle events, settlements, and outcomes.
- Add idempotent writers, payload-hash conflict quarantine, append-only correction fields, archive manifests, and explicit backfill provenance.
- Add point-in-time dataset export that requires `training_as_of` and excludes future labels.
- Add `memory-status`, `memory-report`, `memory-backfill`, `memory-archive`, `memory-dataset`, and memory timeline commands.
- Add a Market Memory UI page and dashboard/settings cards.
- Keep all capture shadow-only and paper/demo only; do not change model, sizing, risk, or execution behavior.

## Phase 4: Live Execution With Strict Risk Controls

- Authenticated API access only after explicit approval.
- Risk limits.
- Position limits.
- Kill switches.
- Separate credentials and deployment controls.
- Full monitoring and incident runbooks.
