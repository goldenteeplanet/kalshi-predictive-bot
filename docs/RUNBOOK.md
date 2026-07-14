# Runbook

## Local Setup

```bash
python -m pip install -e ".[dev]"
```

Optional environment setup:

```bash
cp .env.example .env
```

## One-Time DB Init

```bash
kalshi-bot init-db
```

The default database is `data/kalshi_phase1.db`.

## Database Health And Backends

SQLite is the default. PostgreSQL is supported for longer overnight learning
runs.

```bash
kalshi-bot db-health
kalshi-bot db-doctor
kalshi-bot sqlite-backup
kalshi-bot db-migrate
```

For local PostgreSQL:

```bash
make postgres-up
DB_BACKEND=postgres kalshi-bot db-migrate
DB_BACKEND=postgres kalshi-bot db-health
```

Avoid running SQLite from OneDrive during overnight loops. See
`docs/POSTGRES_SETUP.md` and `docs/DATABASE_RECOVERY.md`.

## Phase 3O Market Memory

Phase 3O captures a durable point-in-time ledger for market state, forecasts,
opportunities, Phase 3M sizing, Phase 3N risk, paper trade lifecycle events,
settlements, and final outcomes. It is enabled in shadow-capture mode by default
and does not change trading behavior.

```bash
kalshi-bot memory-status
kalshi-bot memory-report --output reports/market_memory_report.md
kalshi-bot memory-backfill --dry-run
kalshi-bot memory-archive --output-dir data/memory_archive
```

Backfill existing source tables only after reviewing the dry-run counts:

```bash
kalshi-bot memory-backfill --write
```

Build point-in-time learning datasets only with an explicit training cutoff:

```bash
kalshi-bot memory-dataset --training-as-of 2026-06-18T00:00:00Z --output reports/memory_dataset.json
```

To disable capture without affecting forecasts, paper trades, Phase 3M, Phase 3N,
or Learning Mode:

```bash
PHASE_3O_MARKET_MEMORY_ENABLED=false
```

## Recurring Collection

Run one bounded pass:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
```

For recurring collection, schedule that command with cron, Task Scheduler, or another scheduler. Keep the `--max-pages` value conservative until you understand API volume and local storage growth.

## Report Generation

Sync recently settled markets:

```bash
kalshi-bot sync-settlements --lookback-days 30 --max-pages 5
```

Generate report files:

```bash
kalshi-bot report-calibration --model-name market_implied_v1 --output reports/calibration.md
```

## External Feature Ingestion

Store manually supplied JSON:

```bash
kalshi-bot ingest-external --source weather --input-file path.json
kalshi-bot ingest-external --source crypto --input-file path.json
kalshi-bot ingest-external --source economic --input-file path.json
```

The external ingestion commands do not require credentials and do not call paid APIs.

## Multi-Model Forecasting

```bash
kalshi-bot forecast --model all
```

External models skip cleanly when relevant feature data is missing.

## Backtesting And Comparison

```bash
kalshi-bot backtest --model-name market_implied_v1 --strategy paper_v1 --days 30 --output reports/backtest_market_implied_v1.md
kalshi-bot compare-strategies --days 30 --output reports/strategy_comparison.md
```

Backtests use stored forecasts, snapshots, and settlements only. They do not call live APIs.

## Opportunity Scanner And Leaderboard

```bash
kalshi-bot find-opportunities --model-name market_implied_v1 --limit 20 --output reports/opportunities.md
kalshi-bot market-rankings --limit 50 --output reports/market_rankings.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

Use these reports to prioritize manual review. They are not live-trading signals.

## Crypto Forecasting

Collect public crypto prices, build features, link stored Kalshi markets, and run the crypto model:

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

`ingest-crypto` supports `coinbase` and `coingecko` public endpoints. Forecasts skip markets with no crypto link, low link confidence, missing feature rows, or unusable market prices.

## Weather Forecasting

Collect public NOAA/NWS forecasts, build features, link stored Kalshi markets, and run the weather model:

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

For offline/manual ingestion:

```bash
kalshi-bot ingest-weather --location-key kansas_city --input-file data/weather_sample.json
```

Forecasts skip markets with no weather link, low link confidence, stale forecasts, missing features, unsupported metrics, or unusable market prices.

## Model Tournament And Ensemble v2

Run all models, compare tournament results, generate weights, and then forecast with `ensemble_v2`:

```bash
kalshi-bot forecast --model all
kalshi-bot tournament --days 30 --output reports/model_tournament.md
kalshi-bot model-diagnostics --days 30 --output reports/model_diagnostics.md
kalshi-bot model-weights --days 30 --output reports/model_weights.md
kalshi-bot forecast --model ensemble_v2
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

`ensemble_v2` uses stored category weights when available and falls back to a simple average of stored component forecasts when no usable weights exist. The tournament uses only local forecasts, settlements, paper orders, and backtest trades.

## Decision UI

Start the local demo-only UI:

```bash
kalshi-bot ui
```

Open:

```text
http://127.0.0.1:8080
```

The UI is read-only by default through `UI_READ_ONLY=true`. Demo execution controls stay disabled unless `EXECUTION_ENABLED=true`, `EXECUTION_DRY_RUN=false`, `EXECUTION_KILL_SWITCH=false`, and all risk checks pass.

### Decision Cockpit

The `/opportunities` dashboard is organized around human-readable opportunity cards. Read each card from top to bottom:

1. Market title and model.
2. Simple recommendation: `Bot would buy YES`, `Bot would buy NO`, or `No trade recommended`.
3. Confidence, edge in cents, score out of 100, spread, liquidity, time remaining, and paper position.
4. `Why This Looks Interesting`.
5. `Why This Might Be Risky`.
6. `What the Bot Would Do`.
7. `Recommended Action`.

Badge meanings:

- `Good`: no major local risk stands out.
- `Caution`: review details before acting.
- `Risky`: stronger risk flag present.
- `No Trade`: skip this market.
- `Demo Only`: not a live trading surface.
- `Dry Run`: no order is placed.
- `Stale Data`: local snapshot is older than the freshness limit.
- `Low Edge`: estimated edge is thin.
- `High Spread`: spread may erase edge.
- `Low Liquidity`: fill quality may be weak.

Use `View Full Breakdown` to open the detail page. The tabs separate Forecast, Market Quality, Risk Checks, Paper History, Backtest History, and Raw Data. Raw JSON is collapsed by default under `Advanced / Raw Data`.

Explain one opportunity in the terminal:

```bash
kalshi-bot explain-opportunity --ticker TICKER --model-name ensemble_v2
```

## Demo Autopilot

Check current guardrail configuration and latest state:

```bash
kalshi-bot autopilot-status
```

Run one local dry-run cycle:

```bash
kalshi-bot autopilot-once
```

Generate the Markdown report:

```bash
kalshi-bot autopilot-report --output reports/autopilot_report.md
```

Start the UI and open `/autopilot`:

```bash
kalshi-bot ui
```

The autopilot is disabled by default with `AUTOPILOT_ENABLED=false`. It requires `KALSHI_ENV=demo`, fresh local snapshots, no active kill switch, and all configured limits to pass. In dry-run mode it records attempted orders without calling the demo execution client.

The `/autopilot` page states whether autopilot is off, dry-run only, or blocked. Review `Blocked because`, `Top guardrail blocking trades`, and the checklist before changing settings.

For scheduled local cycles:

```bash
kalshi-bot autopilot-run
```

`AUTOPILOT_MAX_CYCLES=0` means run until a guardrail stops the scheduler or the operator interrupts it. Keep this command supervised while validating settings.

## Overnight Paper Learning

Check current state:

```bash
kalshi-bot overnight-status
```

Run one paper-learning cycle:

```bash
kalshi-bot overnight-once
kalshi-bot overnight-report --output reports/overnight_report.md
```

Run the bounded overnight scheduler:

```bash
OVERNIGHT_ENABLED=true kalshi-bot overnight-run
```

Defaults are conservative:

- `OVERNIGHT_ENABLED=false`: scheduled overnight loops do not start accidentally.
- `OVERNIGHT_MAX_CYCLES=32`: a 15-minute interval gives about eight hours of cycles.
- `OVERNIGHT_RUN_PAPER=true`: the loop can create simulated paper bets.
- `OVERNIGHT_RUN_DEMO=false`: no demo execution is submitted by the overnight loop.
- `OVERNIGHT_STOP_ON_ERROR=false`: individual step errors are stored and the loop continues.

The overnight loop stores data in the same local SQLite database by default. This lets model metrics join against markets, forecasts, paper orders, settlements, and P&L. Use a separate `KALSHI_DB_URL` only if you intentionally want an isolated experiment.

Open the UI and go to `/overnight`:

```bash
kalshi-bot ui
```

Review `What Happened Overnight`, `What Improved`, `What Needs Attention`, `Current Bot Health`, and the model metrics table before changing thresholds.

## Learning Mode And Model Confidence

Learning Mode is enabled by default for higher-volume paper feedback:

```bash
kalshi-bot learning-status
kalshi-bot learning-once
kalshi-bot learning-report --output reports/learning_report.md
kalshi-bot learning-targets --limit 100 --output reports/learning_targets.md
kalshi-bot model-confidence --days 30 --output reports/model_confidence.md
```

For an overnight-style bounded loop:

```bash
kalshi-bot learning-run --max-cycles 32 --interval-minutes 15
```

Learning Mode writes only paper orders. It lowers `PAPER_MIN_EDGE` and opportunity thresholds through Learning Mode settings, keeps paper order quantity small, caps daily paper trades, blocks demo execution, and never adds live execution.

Use the UI pages:

- `/learning`: progress toward settled paper trade targets, recent cycles, targets, and report links.
- `/models/confidence`: model confidence scores and labels.

`model-confidence` writes `model_confidence_v1` weights into `model_weights`. `ensemble_v2` automatically uses the newest stored category weights.

### Forum Consensus

Import aggregate forum-consensus notes from JSON:

```bash
kalshi-bot ingest-forum-consensus --input-file consensus.json
```

The signal is used only for explanation and review. A longshot is flagged when the imported aggregate has enough historically winning participants, enough average win rate, a recent observation, and a price below `FORUM_CONSENSUS_LONGSHOT_MAX_PRICE`.

Example JSON:

```json
{
  "ticker": "EXAMPLE-TICKER",
  "observed_at": "2026-06-16T22:00:00Z",
  "source": "manual_forum_note",
  "side": "YES",
  "participant_count": 20,
  "winner_count": 7,
  "average_win_rate": "0.62",
  "longshot_price": "0.18"
}
```

Use `explain-opportunity` or the UI detail page to see whether a Forum Consensus / Longshot Watch signal is attached.

## Trader Workstation

After an overnight paper-learning run, generate workstation reports:

```bash
kalshi-bot portfolio-summary --output reports/portfolio_summary.md
kalshi-bot daily-briefing --output reports/daily_briefing.md
kalshi-bot analytics-report --output reports/analytics_report.md
```

Start the UI:

```bash
kalshi-bot ui
```

Use these pages for review:

- `/portfolio`: paper portfolio value, exposure, P&L, allocation, and open positions.
- `/positions/{ticker}`: position history, recent fills, forecasts, opportunities, and backtests for one market.
- `/models`: model leaderboard and active model health.
- `/markets`: ranked market monitor with category, model, search, score, liquidity, and confidence filters.
- `/analytics`: P&L, opportunity, forecast, model, and paper-trade trends.
- `/watchlists`: local watchlist creation defaults and ticker membership controls.
- `/alerts`: local alert rules and recent alert events.

The workstation records local portfolio and position snapshots as it is used. Alerts and watchlists are local review tools; they do not place orders.

## Opportunity Intelligence Cockpit

Run the local intelligence flow:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot ui-summary
kalshi-bot ui
```

Open:

```text
http://127.0.0.1:8080
```

Read the cockpit from top to bottom:

- `Today's Summary`: markets scanned, forecasts generated, opportunities found, open paper trades, paper P&L, best model, best opportunity, and autopilot status.
- `Paper Portfolio`: realized/unrealized P&L, open positions, open opportunities, and largest exposure.
- Trade cards: short title, category, BUY YES / BUY NO / NO TRADE, confidence, score, edge, expected value, price, spread, liquidity, time remaining, primary driver, signals, and risks.
- `View Market Details`: full Kalshi title and technical context hidden by default.

Traffic lights:

- `Strong Opportunity`: high score, strong edge, fresh data, and acceptable spread.
- `Watchlist`: enough score or edge to monitor.
- `Avoid`: stale data, low score, high spread, low liquidity, or weak confidence.

The risk meter is a review aid. It summarizes freshness, edge, liquidity, spread, and confidence. It does not place orders.

Use `/opportunities/best-payouts` to review expected-value and payout/risk ranking. Low-confidence longshots are filtered out before the best-payout list is shown.

## Research Assistant

Run the evidence-based research flow after collection, forecasting, and opportunity ranking:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot research-report --model-name ensemble_v2 --limit 10 --output reports/research_report.md
kalshi-bot ask-research "Why is this ranked #1?" --model-name ensemble_v2
kalshi-bot ui
```

Use:

- `kalshi-bot research-opportunity --ticker TICKER --model-name ensemble_v2` for a single-market writeup.
- `kalshi-bot ask-research "What data is missing?" --ticker TICKER --model-name ensemble_v2` for predefined local questions.
- `/research` for the question box, top opportunities explained, top risks, missing-data warnings, and model drivers.
- `/research/opportunity/{ticker}` or the opportunity card `Why?` link for a full analyst-style writeup.

The assistant is deterministic. It does not call external LLM APIs, does not require OpenAI API keys, and does not place trades. If crypto, weather, backtest, leaderboard, snapshot, or settlement data is missing, the writeup says so instead of inventing signals.

`research-report` stores `opportunity_research_snapshots`. Repeated report runs let `ask-research "What changed since last run?" --ticker TICKER --model-name ensemble_v2` compare rank, score, edge, and recommendation changes.

## Signal Marketplace

Run the local signal flow:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot signal-explorer
kalshi-bot signal-leaderboard
kalshi-bot signal-report --output reports/signal_report.md
kalshi-bot ui
```

Use:

- `kalshi-bot signal-explorer` to list available signals, current activity, associated models, and performance.
- `kalshi-bot signal-leaderboard` to rank signals by ROI, sample size, calibration, and confidence.
- `kalshi-bot signal-performance --signal-name "Crypto Signal"` to inspect one signal.
- `/signals` for the local marketplace and signal leaderboard.
- `/signals/{signal_name}` for signal details, recent opportunities, recent trades, recent markets, top/worst markets, and research summary.

Forecast signals are attributed when forecasts are generated through `forecast` or `collect-once`. Paper-trade signals are attributed when paper orders are created. Signal performance is refreshed by signal reports, leaderboards, explorer runs, and paper P&L calculation.

Signal ROI and P&L are diagnostic. One paper outcome can be attributed to multiple active signals, because signals are evidence contributors, not mutually exclusive strategies. Signals with small sample sizes remain visible as `Insufficient Data`.

## News Intelligence

Run the local news flow:

```bash
kalshi-bot ingest-news --source rss
kalshi-bot link-news-markets
kalshi-bot build-news-features --window-minutes 360
kalshi-bot forecast --model news_v1
kalshi-bot news-report --output reports/news_report.md
kalshi-bot news-opportunities --model-name news_v1 --limit 20 --output reports/news_opportunities.md
kalshi-bot news-backtest --days 30 --output reports/news_backtest.md
kalshi-bot ui
```

If RSS feeds are not configured, import manual JSON/CSV instead:

```bash
kalshi-bot ingest-news --input-file data/news_sample.json
kalshi-bot ingest-news --input-file data/news_sample.csv
```

Use `/news` for the News Intelligence dashboard and `/news/{id}` for item-level detail.
The news pipeline is local and deterministic. It uses stored news and stored markets only
after ingestion, does not call external LLM APIs, and does not place trades.

## Sports Intelligence

Run the local sports flow:

```bash
kalshi-bot ingest-sports --league MLB --input-file data/mlb_sample.json
kalshi-bot link-sports-markets --league ALL
kalshi-bot build-sports-features --league ALL
kalshi-bot forecast --model sports_v1
kalshi-bot sports-report --league ALL --output reports/sports_report.md
kalshi-bot sports-opportunities --model-name sports_v1 --league ALL --limit 20 --output reports/sports_opportunities.md
kalshi-bot sports-backtest --league ALL --days 30 --output reports/sports_backtest.md
kalshi-bot ui
```

Use `/sports` for the Sports Intelligence dashboard, `/sports/leagues/{league}` for league
views, and `/sports/games/{game_key}` for a game-level feature and market-link view.

Sports import starts with manual JSON/CSV files. JSON supports `league`, `teams`, `games`,
`team_stats`, `injuries`, and `odds`. CSV supports `record_type` rows for `team`, `game`,
`team_stat`, `injury`, and `odds`. Provider scaffolding is public/free only and does not require
paid sports APIs.

For a 10-minute advisory profile:

```bash
kalshi-bot scheduler-plan --profile sports-watch
```

The sports pipeline is local and deterministic. It uses stored sports data and stored markets only
after ingestion, does not place trades, and keeps every output paper/demo only.

## Market Microstructure

Run the local microstructure flow after collecting snapshots:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot build-microstructure-features --lookback-minutes 60
kalshi-bot forecast --model microstructure_v1
kalshi-bot microstructure-report --output reports/microstructure_report.md
kalshi-bot microstructure-opportunities --model-name microstructure_v1 --limit 20 --output reports/microstructure_opportunities.md
kalshi-bot microstructure-backtest --days 30 --output reports/microstructure_backtest.md
kalshi-bot ui
```

Use `/microstructure` for the dashboard and `/microstructure/{ticker}` for ticker-level spread,
liquidity, midpoint, imbalance, event, and signal context. Opportunity cards also show the latest
microstructure summary when available.

The feature builder reads only local `market_snapshots` and stored orderbook JSON. It does not call
live APIs. Possible informed flow is a heuristic from market movement, spread, liquidity, imbalance,
late timing, and dislocation; it is not proof of smart money.

For a 5-minute advisory profile:

```bash
kalshi-bot scheduler-plan --profile microstructure-watch
```

The microstructure pipeline is local and deterministic. It does not place trades and keeps every
output paper/demo only.

## Meta Model

Run the local meta model flow after forecasts and feature builders have populated local data:

```bash
kalshi-bot build-meta-features --model-scope all
kalshi-bot build-meta-training --days 90
kalshi-bot forecast --model meta_model_v1
kalshi-bot forecast --model meta_ensemble_v1
kalshi-bot meta-evaluate --days 90 --output reports/meta_evaluation.md
kalshi-bot meta-report --output reports/meta_report.md
kalshi-bot meta-opportunities --limit 20 --output reports/meta_opportunities.md
kalshi-bot ui
```

Use `/meta` for the dashboard and `/meta/{ticker}` for ticker-level model selection context.
Opportunity cards show the trusted model, trust score, and fallback badge when evidence is thin.

For a 15-minute advisory profile:

```bash
kalshi-bot scheduler-plan --profile meta-watch
```

The meta selector is local and deterministic. It predicts which model to trust, not whether to
place a real order. All meta forecasts, decisions, reports, and signals remain paper/demo only.

## Troubleshooting

### API 429

The client retries 429 responses with exponential backoff and honors numeric `Retry-After` values. Reduce `--limit`, `--max-pages`, or collection frequency if throttling persists.

### API 5xx

The client retries transient 5xx responses. Re-run the collection pass if the API remains temporarily unavailable.

### Database Path Problems

Check `KALSHI_DB_URL`. The default is:

```text
sqlite:///data/kalshi_phase1.db
```

For a custom SQLite path, ensure the parent directory is writable.

### No Forecasts

The market-implied forecaster skips markets with no usable orderbook, bid/ask, or last-price field.

### No Crypto Forecasts

Run `link-crypto-markets` after syncing markets, then run `ingest-crypto` and `build-crypto-features`. `crypto_v2` also requires at least `CRYPTO_V2_MIN_HISTORY_MINUTES` of price history for momentum features.

### No Weather Forecasts

Run `link-weather-markets` after syncing markets, then run `ingest-weather` and `build-weather-features`. `weather_v2` also requires link confidence at or above `WEATHER_V2_MIN_LINK_CONFIDENCE` and forecast age no older than `WEATHER_V2_MAX_FORECAST_AGE_HOURS`.

### No Ensemble v2 Forecasts

Run component forecasts first with `kalshi-bot forecast --model all` or run individual component models. `ensemble_v2` skips when no stored component forecasts exist for the same ticker/snapshot.

### No News Forecasts

Run `ingest-news`, `link-news-markets`, and `build-news-features` first. `news_v1` skips
markets that have no news feature row, no usable market midpoint, or no linked news.

### No Sports Forecasts

Run `ingest-sports`, `link-sports-markets`, and `build-sports-features` first. Sports models
skip markets that have no sports market link, no sports feature row, or no usable market midpoint.

### No Microstructure Forecasts

Run `collect-once --include-orderbook` through the normal collection job, then run
`build-microstructure-features --lookback-minutes 60`. `microstructure_v1` skips markets that do not
have enough recent snapshots, have no microstructure feature row, or have no usable market midpoint.

### UI Shows No Opportunities

Run `find-opportunities` first. The dashboard reads local `market_rankings` rows and does not call live APIs.

### Autopilot Is Blocked

Run:

```bash
kalshi-bot autopilot-report --output reports/autopilot_report.md
```

Review the latest `risk_events` section. Common causes are `AUTOPILOT_ENABLED=false`, `KALSHI_ENV` not set to `demo`, stale snapshots, low edge, low opportunity score, daily order limits, duplicate ticker/model/side attempts, drawdown breaches, or `EXECUTION_KILL_SWITCH=true`.

### Empty Tournament Report

Run forecasts, sync settlements, and run backtests before expecting meaningful rankings. Sparse models are intentionally shown as `INSUFFICIENT_DATA`.

### Empty Calibration Report

Calibration requires both forecasts and matching settlement rows. Run collection first, wait for some markets to settle, then run `sync-settlements` before generating the report.
