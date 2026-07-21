# kalshi-predictive-bot

`kalshi-predictive-bot` is a Phase 1 read-only forecasting foundation for Kalshi public market data.

It collects public market metadata and orderbook snapshots, stores raw JSON locally for auditability, generates a baseline market-implied forecast, and produces calibration/evaluation reports after settlements are synced.

## Current Next Phase: GH-2 Active Candidate Decision Refresh

GH-2 closes the cloud live-data paper-decision loop without creating paper or
exchange orders. The reconnecting GH-1 watch now prioritizes tickers from
current actionable rankings, then uses bounded series discovery as a fallback.
A separate 15-minute service stages public crypto quotes in parallel and drains
orderbooks, features, links, forecasts, rankings, and opportunities through the
shared single-writer lock.

```bash
# Filesystem-only parallel fetch stage; does not open SQLite.
kalshi-bot gh2-stage-crypto-quotes \
  --staging-dir reports/phase_gh2/crypto_staging

# One bounded writer cycle; paper-order and live execution paths stay disabled.
kalshi-bot gh2-single-writer-decision-refresh \
  --apply \
  --output-dir reports/phase_gh2 \
  --reports-dir reports \
  --candidate-manifest-path reports/phase_gh1/watch/actionable_tickers.json
```

The cloud timer definition is
`deploy/systemd/kalshi-gh2-decision-refresh.timer`. A soak cycle receives credit
only when it produces a fresh ranked candidate, encounters no stage errors, and
creates zero paper orders. Even after 24 consecutive healthy cycles with a
paper-ready candidate, paper-order creation still requires explicit operator
approval. Live execution and autopilot remain disabled.

Phase 2 adds a paper trading ledger. It creates simulated orders and immediate simulated fills from stored forecasts/snapshots only. It still does not authenticate with Kalshi or place real orders.

Phase 2.5 adds a feature store, manual external data ingestion scaffolding, a model registry, historical backtesting, and strategy comparison. It remains read-only and simulated.

Phase 2.6 adds an opportunity scanner, market ranking reports, and a model leaderboard. These are diagnostic reports only and still do not place trades.

Phase 2.7 adds public crypto price ingestion, crypto-specific features, crypto-market linking, a `crypto_v2` forecaster, and crypto backtest/reporting commands. It still does not authenticate with Kalshi or place real orders.

Phase 2.8 adds public NOAA/NWS weather ingestion, weather-specific features, weather-market linking, a `weather_v2` forecaster, and weather reports/backtests. It remains read-only and simulated.

Phase GH-1 adds a disabled-by-default, read-only Kalshi WebSocket orderbook
adapter. It reconstructs snapshots and deltas, detects sequence gaps, recovers
from the public REST orderbook, calculates depth/imbalance/executable prices,
stages snapshots on disk, and persists them only through a guarded single
writer. Kalshi requires authenticated WebSocket handshakes, but this adapter
has no order, cancel, portfolio, or execution methods.

```bash
# No connection is made unless --connect is supplied and the feature is enabled.
kalshi-bot gh1-websocket-orderbook --tickers TICKER1,TICKER2 --dry-run

# Inspect staged files and the writer gate without writing.
kalshi-bot gh1-websocket-orderbook-drain --diagnose-only
```

Phase 2.9 adds a model tournament, model diagnostics, category-specific model weights, and `ensemble_v2`. It compares local models from stored forecasts/backtests only and keeps Phase 3 blocked until the tournament evidence is strong enough.

Phase 3A adds a local decision review UI for inspecting opportunities before any future demo execution path. It runs only on localhost and stays demo-only.

Phase 3B adds a local demo autopilot with persisted runs, cycles, guardrail risk events, dry-run defaults, and an Autopilot UI page. It does not add production live trading.

Phase 3C adds a human-readable decision cockpit, plain-English opportunity explanations, report cards, redesigned detail/autopilot pages, and an `explain-opportunity` CLI command. It remains demo-only and paper-only.

Phase 3C.5 adds a safe overnight paper-learning loop. It collects data, runs the model stack, finds opportunities, creates paper bets, stores paper P&L and iteration metrics, and generates an overnight report. It also adds imported forum-consensus longshot signals. It remains paper-only by default and does not place real orders.

Phase 3D adds a local trader workstation for paper positions, portfolio snapshots, model performance, market monitoring, analytics, watchlists, alerts, and daily briefing reports. It remains paper-only and demo-only.

Phase 3E adds payout-adjusted opportunity intelligence and a more human-readable trader cockpit. It introduces trade cards, traffic-light labels, risk meters, short market names, best-payout ranking, and a plain-English UI summary. It remains paper-only and demo-only.

Phase 3F adds a local deterministic Research Assistant. It explains why opportunities are ranked, what signals support them, what could go wrong, what data is missing, and what to do next. It does not call external LLM APIs or enable live trading.

Phase 3F-1 adds Learning Mode and a Model Confidence Engine. It increases paper-only data capture, prioritizes fast-settling targets, scores models from settled outcomes, and feeds confidence weights into `ensemble_v2`. It blocks demo execution while Learning Mode is active and does not add live trading.

Phase 3G adds a Signal Marketplace. It tracks which deterministic signals contributed to forecasts, opportunities, paper trades, and paper/demo performance so the system can ask which signals actually make money.

The database hardening layer adds SQLite/PostgreSQL backend selection, safer SQLite defaults, Alembic migration scaffolding, DB health/doctor commands, SQLite backup/recovery commands, and UI database status. It does not change trading behavior.

Phase 3H adds News Intelligence. It ingests RSS or manual JSON/CSV news, classifies items locally, links news to markets, creates news signals, adds `news_v1`, and reports whether news-driven paper/demo signals help.

Phase 3J adds Sports Intelligence. It imports manual JSON/CSV sports data, links sports markets to games, builds bounded sports features and sports signals, adds league-specific sports models plus `sports_v1`, and exposes sports UI/reporting. It uses public/free scaffolding only and does not add live trading.

Phase 3K adds a Market Microstructure Engine. It analyzes stored snapshots and orderbook JSON for spread changes, liquidity changes, orderbook imbalance, price dislocations, late moves, and cautious possible informed-flow heuristics. It adds `microstructure_v1`, microstructure reports, Signal Marketplace integration, and a Microstructure UI page. It remains paper/demo only.

Phase 3L adds a Meta Model. It predicts which forecasting model to trust for each market, category, time horizon, and data condition, then adds `meta_model_v1`, `meta_ensemble_v1`, meta reports, Meta Model UI pages, and meta signals. It remains paper/demo only.

Phase 3O adds Market Memory. It captures point-in-time market, forecast, opportunity, sizing/risk, paper trade, settlement, and outcome events into durable memory stores for future learning. It remains paper/demo only and does not change trading behavior.

## Phase 1 Scope

- Public Kalshi market metadata collection.
- Public Kalshi orderbook snapshot collection.
- Local SQLite persistence by default.
- Raw JSON storage for reproducibility and auditability.
- Baseline market-implied probability forecasts.
- Settlement sync from public settled market data.
- Calibration and row-level evaluation reports.
- Paper trading decision engine.
- Simulated paper fills and position ledger.
- Paper P&L snapshots and Markdown reports.
- Feature snapshots and external feature JSON storage.
- Multi-model forecast runs and ensemble forecasts.
- Historical backtests and strategy comparison reports.
- Opportunity scanner and market ranking report.
- Model leaderboard report.
- Public Coinbase/CoinGecko crypto price ingestion.
- Crypto feature tables, crypto-market links, and `crypto_v2` forecasts.
- Crypto feature and crypto backtest reports.
- Public NOAA/NWS weather forecast ingestion.
- Weather feature tables, weather-market links, and `weather_v2` forecasts.
- Weather feature and weather backtest reports.
- Model tournament runs, diagnostics, generated weights, and `ensemble_v2`.
- Local FastAPI decision review UI.
- Demo-only autopilot runs, cycles, risk events, and reports.
- Human-readable opportunity cards, explanation tabs, badge meanings, and CLI explanations.
- Overnight paper-learning runs, cycles, model iteration metrics, and reports.
- Imported forum-consensus longshot signals for aggregate winner-contingent review.
- Trader workstation pages, position history, portfolio snapshots, watchlists, alerts, and analytics reports.
- Payout-adjusted opportunity scores, best-payout reports, traffic-light trade cards, and cockpit summaries.
- Learning Mode runs, cycles, target reports, confidence scores, and dynamic `ensemble_v2` confidence weights.
- News tables, RSS/manual ingestion, market linking, news features, news signals, `news_v1`, and news reports.
- Sports tables, manual JSON/CSV ingestion, sports-market links, sports features, sports signals, league sports models, `sports_v1`, and sports reports.
- Microstructure feature/event/signal tables, orderbook depth snapshots, `microstructure_v1`, and microstructure reports.
- Meta model feature, decision, training, and performance tables; `meta_model_v1`; `meta_ensemble_v1`; meta reports; meta signals; and Meta Model UI pages.
- SQLite/PostgreSQL backend selection, database health checks, Alembic migration scaffolding, and SQLite backup/recovery tooling.
- Phase 3O `market_memory`, `forecast_memory`, `trade_memory`, archive manifests, quarantine logs, status/report/backfill/archive/dataset commands, and a Market Memory UI page.

## Intentionally Not Included

- Live trading.
- Order placement code.
- Private or authenticated Kalshi requests.
- API keys, private keys, request signing, portfolio access, balances, positions, or order management.
- Execution engines.
- Risk-managed live execution.
- Any real order routing from paper orders.

## Setup

Requires Python 3.11+.

```bash
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` if you want to override defaults.

## Environment Variables

| Variable | Default |
|---|---|
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com/trade-api/v2` |
| `KALSHI_ENV` | `demo` |
| `KALSHI_DB_URL` | `sqlite:///data/kalshi_phase1.db` |
| `KALSHI_REQUEST_TIMEOUT_SECONDS` | `15` |
| `KALSHI_MAX_RETRIES` | `3` |
| `KALSHI_RETRY_BACKOFF_SECONDS` | `1.0` |
| `KALSHI_USER_AGENT` | `kalshi-predictive-bot/phase1` |
| `LOG_LEVEL` | `INFO` |
| `PAPER_MIN_EDGE` | `0.05` |
| `PAPER_MAX_ORDER_QUANTITY` | `1` |
| `PAPER_MAX_POSITION_PER_MARKET` | `5` |
| `PAPER_MAX_OPEN_ORDERS` | `100` |
| `PAPER_DEFAULT_FEE_PER_CONTRACT` | `0` |
| `PAPER_ALLOW_BUY_NO` | `true` |
| `PAPER_ALLOW_SELLING` | `false` |
| `PAPER_ORDER_TTL_MINUTES` | `120` |
| `DYNAMIC_POSITION_SIZING_MODE` | `disabled` |
| `DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS` | `1` |
| `DYNAMIC_POSITION_SIZING_GLOBAL_MAX_CONTRACTS` | `5` |
| `DYNAMIC_POSITION_SIZING_EXTERNAL_RISK_CAP` | unset |
| `DYNAMIC_POSITION_SIZING_MARGIN_CAP` | unset |
| `DYNAMIC_POSITION_SIZING_PORTFOLIO_CAP` | unset |
| `OPPORTUNITY_MIN_EDGE` | `0.03` |
| `OPPORTUNITY_MIN_SCORE` | `60` |
| `OPPORTUNITY_MAX_SPREAD` | `0.10` |
| `OPPORTUNITY_MIN_LIQUIDITY` | `0` |
| `OPPORTUNITY_MIN_TIME_TO_CLOSE_MINUTES` | `30` |
| `OPPORTUNITY_MAX_RESULTS` | `20` |
| `CRYPTO_V2_MAX_ADJUSTMENT` | `0.08` |
| `CRYPTO_V2_MIN_LINK_CONFIDENCE` | `0.6` |
| `CRYPTO_V2_MIN_HISTORY_MINUTES` | `60` |
| `WEATHER_V2_MAX_ADJUSTMENT` | `0.10` |
| `WEATHER_V2_MIN_LINK_CONFIDENCE` | `0.6` |
| `WEATHER_V2_MAX_FORECAST_AGE_HOURS` | `24` |
| `WEATHER_V2_DEFAULT_LOCATION_KEY` | `kansas_city` |
| `UI_READ_ONLY` | `true` |
| `EXECUTION_ENABLED` | `false` |
| `EXECUTION_DRY_RUN` | `true` |
| `EXECUTION_KILL_SWITCH` | `false` |
| `EXECUTION_CONFIRMATION_TOKEN` | `DEMO ONLY` |
| `AUTOPILOT_ENABLED` | `false` |
| `AUTOPILOT_DRY_RUN` | `true` |
| `AUTOPILOT_MODEL` | `ensemble_v2` |
| `AUTOPILOT_INTERVAL_SECONDS` | `300` |
| `AUTOPILOT_MAX_CYCLES` | `0` |
| `AUTOPILOT_MAX_ORDERS_PER_CYCLE` | `1` |
| `AUTOPILOT_MAX_DAILY_ORDERS` | `10` |
| `AUTOPILOT_MIN_EDGE` | `0.05` |
| `AUTOPILOT_MIN_OPPORTUNITY_SCORE` | `75` |
| `AUTOPILOT_STOP_ON_DRAWDOWN` | `true` |
| `AUTOPILOT_MAX_DAILY_DRAWDOWN` | `5.00` |
| `AUTOPILOT_MAX_OPEN_DEMO_ORDERS` | `5` |
| `AUTOPILOT_REQUIRE_FRESH_DATA_MINUTES` | `15` |
| `OVERNIGHT_ENABLED` | `false` |
| `OVERNIGHT_INTERVAL_MINUTES` | `15` |
| `OVERNIGHT_MAX_CYCLES` | `32` |
| `OVERNIGHT_MODEL` | `ensemble_v2` |
| `OVERNIGHT_RUN_PAPER` | `true` |
| `OVERNIGHT_RUN_DEMO` | `false` |
| `OVERNIGHT_RUN_BACKTEST` | `true` |
| `OVERNIGHT_RUN_REPORTS` | `true` |
| `OVERNIGHT_MIN_FREE_DISK_MB` | `500` |
| `OVERNIGHT_STOP_ON_ERROR` | `false` |
| `OVERNIGHT_REQUIRE_MARKET_DATA` | `true` |
| `FORUM_CONSENSUS_ENABLED` | `true` |
| `FORUM_CONSENSUS_MIN_WINNERS` | `5` |
| `FORUM_CONSENSUS_MIN_WIN_RATE` | `0.55` |
| `FORUM_CONSENSUS_LONGSHOT_MAX_PRICE` | `0.25` |
| `FORUM_CONSENSUS_MAX_AGE_HOURS` | `24` |
| `NEWS_ENABLED` | `false` |
| `NEWS_DEFAULT_WINDOW_MINUTES` | `360` |
| `NEWS_MAX_ITEMS_PER_FEED` | `50` |
| `NEWS_MIN_IMPORTANCE_SCORE` | `0.40` |
| `NEWS_MIN_LINK_CONFIDENCE` | `0.50` |
| `NEWS_RSS_FEEDS_JSON` | `""` |
| `NEWS_USER_AGENT` | `kalshi-predictive-bot-news/phase3h` |
| `NEWS_V1_MAX_ADJUSTMENT` | `0.06` |
| `LEARNING_MODE` | `true` |
| `LEARNING_TARGET_SETTLED_TRADES` | `500` |
| `LEARNING_MIN_EDGE` | `0.01` |
| `LEARNING_MIN_OPPORTUNITY_SCORE` | `35` |
| `LEARNING_MAX_PAPER_ORDER_QTY` | `1` |
| `LEARNING_MAX_PAPER_POSITIONS_PER_MARKET` | `3` |
| `LEARNING_MAX_DAILY_PAPER_TRADES` | `100` |
| `LEARNING_MIN_TRADES_PER_CYCLE` | `5` |
| `LEARNING_TARGET_TRADES_PER_CYCLE` | `10` |
| `LEARNING_PRIORITIZE_FAST_SETTLEMENT` | `true` |
| `LEARNING_MAX_DAYS_TO_SETTLEMENT` | `3` |
| `LEARNING_ALLOWED_CATEGORIES` | `crypto,weather,economic,general` |
| `LEARNING_BLOCK_DEMO_EXECUTION` | `true` |
| `LEARNING_BLOCK_LIVE_EXECUTION` | `true` |
| `LEARNING_INCLUDE_WATCHLIST` | `true` |
| `LEARNING_MIN_LIQUIDITY` | `0` |
| `LEARNING_MAX_SPREAD` | `0.15` |
| `LEARNING_DUPLICATE_COOLDOWN_HOURS` | `24` |
| `LEARNING_CANDIDATE_SCAN_LIMIT` | `500` |
| `MODEL_CONFIDENCE_MIN_SETTLED_TRADES` | `25` |
| `MODEL_CONFIDENCE_EXPLORATION_WEIGHT` | `0.10` |
| `SPORTS_ENABLED` | `false` |
| `SPORTS_LEAGUES` | `MLB,NBA,NFL,NHL` |
| `SPORTS_DEFAULT_LOOKAHEAD_DAYS` | `7` |
| `SPORTS_DEFAULT_LOOKBACK_DAYS` | `30` |
| `SPORTS_MIN_LINK_CONFIDENCE` | `0.50` |
| `SPORTS_MIN_SIGNAL_CONFIDENCE` | `0.40` |
| `SPORTS_USER_AGENT` | `kalshi-predictive-bot-sports/phase3j` |
| `SPORTS_ODDS_ENABLED` | `false` |
| `SPORTS_WEATHER_ENABLED` | `true` |
| `SPORTS_V1_MAX_ADJUSTMENT` | `0.08` |
| `MLB_V1_MAX_ADJUSTMENT` | `0.08` |
| `NBA_V1_MAX_ADJUSTMENT` | `0.08` |
| `NFL_V1_MAX_ADJUSTMENT` | `0.08` |
| `NHL_V1_MAX_ADJUSTMENT` | `0.08` |
| `MICROSTRUCTURE_ENABLED` | `true` |
| `MICROSTRUCTURE_LOOKBACK_MINUTES` | `60` |
| `MICROSTRUCTURE_SHORT_LOOKBACK_MINUTES` | `15` |
| `MICROSTRUCTURE_MIN_SNAPSHOTS` | `3` |
| `MICROSTRUCTURE_SPREAD_WIDEN_THRESHOLD` | `0.05` |
| `MICROSTRUCTURE_SPREAD_TIGHTEN_THRESHOLD` | `0.03` |
| `MICROSTRUCTURE_LIQUIDITY_CHANGE_THRESHOLD` | `0.25` |
| `MICROSTRUCTURE_IMBALANCE_THRESHOLD` | `0.60` |
| `MICROSTRUCTURE_LATE_MOVE_THRESHOLD` | `0.08` |
| `MICROSTRUCTURE_DISLOCATION_THRESHOLD` | `0.05` |
| `MICROSTRUCTURE_SMART_MONEY_THRESHOLD` | `0.70` |
| `MICROSTRUCTURE_V1_MAX_ADJUSTMENT` | `0.06` |
| `PHASE_3O_MARKET_MEMORY_ENABLED` | `true` |
| `PHASE_3O_MARKET_MEMORY_MODE` | `shadow_capture` |
| `PHASE_3O_SCHEMA_VERSION` | `1` |
| `PHASE_3O_DEFAULT_DATA_MODE` | `AS_OBSERVED` |
| `PHASE_3O_FORECAST_LABEL_POLICY_ID` | `kalshi_binary_result` |
| `PHASE_3O_FORECAST_LABEL_POLICY_VERSION` | `v1` |
| `PHASE_3O_ARCHIVE_DIR` | `data/memory_archive` |

## CLI Usage

```bash
kalshi-bot init-db
kalshi-bot sync-markets --status open --limit 100 --max-pages 1
kalshi-bot snapshot --status open --limit 100 --max-pages 1 --include-orderbook
kalshi-bot forecast --limit 100
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot sync-settlements --lookback-days 30
kalshi-bot report-calibration --model-name market_implied_v1 --output reports/calibration.md
kalshi-bot gh4-paper-activation-preflight
kalshi-bot paper-summary --output reports/paper_trading.md
kalshi-bot paper-pnl
kalshi-bot paper-reset --yes
kalshi-bot ingest-external --source weather --input-file path.json
kalshi-bot forecast --model all
kalshi-bot backtest --model-name market_implied_v1 --strategy paper_v1 --days 30 --output reports/backtest_market_implied_v1.md
kalshi-bot compare-strategies --days 30 --output reports/strategy_comparison.md
kalshi-bot find-opportunities --model-name market_implied_v1 --limit 20 --output reports/opportunities.md
kalshi-bot market-rankings --limit 50 --output reports/market_rankings.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
kalshi-bot ingest-crypto --symbols BTC,ETH --source coinbase
kalshi-bot build-crypto-features --symbols BTC,ETH
kalshi-bot link-crypto-markets
kalshi-bot forecast --model crypto_v2
kalshi-bot find-opportunities --model-name crypto_v2 --limit 20 --output reports/opportunities_crypto_v2.md
kalshi-bot crypto-report --symbols BTC,ETH --output reports/crypto_features.md
kalshi-bot crypto-backtest --days 30 --output reports/crypto_backtest.md
kalshi-bot ingest-weather --location-key kansas_city --lat 39.0997 --lon -94.5786
kalshi-bot build-weather-features --location-key kansas_city
kalshi-bot link-weather-markets
kalshi-bot forecast --model weather_v2
kalshi-bot find-opportunities --model-name weather_v2 --limit 20 --output reports/opportunities_weather_v2.md
kalshi-bot weather-report --location-key kansas_city --output reports/weather_features.md
kalshi-bot weather-backtest --days 30 --output reports/weather_backtest.md
kalshi-bot tournament --days 30 --output reports/model_tournament.md
kalshi-bot model-diagnostics --days 30 --output reports/model_diagnostics.md
kalshi-bot model-weights --days 30 --output reports/model_weights.md
kalshi-bot forecast --model ensemble_v2
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot explain-opportunity --ticker TICKER --model-name ensemble_v2
kalshi-bot autopilot-status
kalshi-bot autopilot-once
kalshi-bot autopilot-report --output reports/autopilot_report.md
kalshi-bot ingest-forum-consensus --input-file consensus.json
kalshi-bot overnight-status
kalshi-bot overnight-once
kalshi-bot overnight-report --output reports/overnight_report.md
kalshi-bot overnight-run
kalshi-bot learning-status
kalshi-bot learning-once
kalshi-bot learning-run --max-cycles 32 --interval-minutes 15
kalshi-bot learning-report --output reports/learning_report.md
kalshi-bot learning-targets --limit 100 --output reports/learning_targets.md
kalshi-bot model-confidence --days 30 --output reports/model_confidence.md
kalshi-bot portfolio-summary --output reports/portfolio_summary.md
kalshi-bot daily-briefing --output reports/daily_briefing.md
kalshi-bot analytics-report --output reports/analytics_report.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot research-opportunity --ticker TICKER --model-name ensemble_v2
kalshi-bot ask-research "Why is this ranked #1?" --model-name ensemble_v2
kalshi-bot research-report --model-name ensemble_v2 --limit 10 --output reports/research_report.md
kalshi-bot signal-explorer
kalshi-bot signal-leaderboard
kalshi-bot signal-performance --signal-name "Crypto Signal"
kalshi-bot signal-report --output reports/signal_report.md
kalshi-bot ingest-news --source rss
kalshi-bot ingest-news --input-file data/news_sample.json
kalshi-bot link-news-markets
kalshi-bot build-news-features --window-minutes 360
kalshi-bot forecast --model news_v1
kalshi-bot news-report --output reports/news_report.md
kalshi-bot news-opportunities --model-name news_v1 --limit 20 --output reports/news_opportunities.md
kalshi-bot news-backtest --days 30 --output reports/news_backtest.md
kalshi-bot ingest-sports --league MLB --input-file data/mlb_sample.json
kalshi-bot link-sports-markets --league ALL
kalshi-bot build-sports-features --league ALL
kalshi-bot forecast --model sports_v1
kalshi-bot sports-report --league ALL --output reports/sports_report.md
kalshi-bot sports-opportunities --model-name sports_v1 --league ALL --limit 20 --output reports/sports_opportunities.md
kalshi-bot sports-backtest --league ALL --days 30 --output reports/sports_backtest.md
kalshi-bot scheduler-plan --profile sports-watch
kalshi-bot build-microstructure-features --lookback-minutes 60
kalshi-bot forecast --model microstructure_v1
kalshi-bot microstructure-report --output reports/microstructure_report.md
kalshi-bot microstructure-opportunities --model-name microstructure_v1 --limit 20 --output reports/microstructure_opportunities.md
kalshi-bot microstructure-backtest --days 30 --output reports/microstructure_backtest.md
kalshi-bot scheduler-plan --profile microstructure-watch
kalshi-bot build-meta-features --model-scope all
kalshi-bot build-meta-training --days 90
kalshi-bot forecast --model meta_model_v1
kalshi-bot forecast --model meta_ensemble_v1
kalshi-bot meta-evaluate --days 90 --output reports/meta_evaluation.md
kalshi-bot meta-report --output reports/meta_report.md
kalshi-bot meta-opportunities --limit 20 --output reports/meta_opportunities.md
kalshi-bot scheduler-plan --profile meta-watch
kalshi-bot ui-summary
kalshi-bot ui
```

## Tests and Quality Gates

```bash
pytest
ruff check .
mypy src
```

Or with Make:

```bash
make test
make lint
make typecheck
```

## One Collection Pass

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
```

This initializes the database if needed, syncs open markets, captures snapshots, creates market-implied forecasts, and prints a summary.

## Calibration Report

After some forecasted markets have settled:

```bash
kalshi-bot sync-settlements --lookback-days 30 --max-pages 5
kalshi-bot report-calibration --model-name market_implied_v1 --output reports/calibration.md
```

The report command writes both a Markdown summary and a row-level CSV next to it.

## Paper Trading

Run a collection pass first so paper trading has forecasts and snapshots:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot gh4-paper-activation-preflight
kalshi-bot paper-summary --output reports/paper_trading.md
kalshi-bot paper-pnl
```

`paper-run` scans the latest forecast per ticker, compares the model probability to stored executable paper prices, creates simulated paper orders when edge clears the configured threshold, immediately fills those simulated orders, and updates paper positions. It is blocked by default and requires a passing GH-4 preflight, `PAPER_ORDER_CREATION_ENABLED=true`, a released `PAPER_ORDER_KILL_SWITCH`, and the exact operator approval token. See [GH-4 Paper Order Activation](docs/GH4_PAPER_ORDER_ACTIVATION.md).

`paper-reset --yes` deletes only paper trading rows. It does not delete market, snapshot, forecast, or settlement data.

## Overnight Paper Learning

Run one safe cycle:

```bash
kalshi-bot overnight-once
kalshi-bot overnight-report --output reports/overnight_report.md
```

Run the bounded overnight scheduler:

```bash
OVERNIGHT_ENABLED=true kalshi-bot overnight-run
```

The loop stores `overnight_runs`, `overnight_cycles`, and `model_iteration_metrics` in the same local database as markets, forecasts, paper orders, and P&L so the data can be joined during review. It creates paper bets when `OVERNIGHT_RUN_PAPER=true`; it does not submit demo or real orders by default.

The `/overnight` UI page summarizes what happened, what improved, what needs attention, current health, paper P&L, cycle history, and model metrics.

## Phase 3F-1 Learning Mode And Model Confidence

Run a one-cycle paper-only learning pass:

```bash
kalshi-bot learning-once
kalshi-bot learning-report --output reports/learning_report.md
```

Run model confidence scoring and target generation:

```bash
kalshi-bot model-confidence --days 30 --output reports/model_confidence.md
kalshi-bot learning-targets --limit 100 --output reports/learning_targets.md
```

Learning Mode stores `learning_runs`, `learning_cycles`, `learning_trade_targets`, and `model_confidence_scores`. It is enabled by default, lowers thresholds only for paper orders, caps daily paper trades, blocks demo execution, and prioritizes markets likely to settle within `LEARNING_MAX_DAYS_TO_SETTLEMENT`.

The confidence engine labels models as `Leader`, `Promising`, `Needs More Data`, or `Underperforming`. It writes `model_confidence_v1` category weights to `model_weights`, which `ensemble_v2` already reads.

The UI adds `/learning` and `/models/confidence`.

## Forum Consensus Signals

Forum consensus is imported from local JSON, not scraped automatically:

```bash
kalshi-bot ingest-forum-consensus --input-file consensus.json
```

Example:

```json
{
  "signals": [
    {
      "ticker": "EXAMPLE-TICKER",
      "observed_at": "2026-06-16T22:00:00Z",
      "source": "manual_forum_note",
      "side": "YES",
      "participant_count": 20,
      "winner_count": 7,
      "average_win_rate": "0.62",
      "longshot_price": "0.18",
      "notes": "Aggregate note only; no individual user data."
    }
  ]
}
```

If enough historically winning participants support a configured longshot, opportunity explanations show a Forum Consensus / Longshot Watch signal. It is a review signal, not an automatic execution trigger.

## Phase 3D Trader Workstation

Run a paper-learning cycle, then open the workstation:

```bash
kalshi-bot overnight-once
kalshi-bot portfolio-summary --output reports/portfolio_summary.md
kalshi-bot daily-briefing --output reports/daily_briefing.md
kalshi-bot analytics-report --output reports/analytics_report.md
kalshi-bot ui
```

Open `http://127.0.0.1:8080/portfolio` or use the left navigation for `/models`, `/markets`, `/analytics`, `/watchlists`, and `/alerts`.

The workstation records local `position_history`, `portfolio_snapshots`, watchlist rows, and alert events. It reads paper orders and local market/model data only; it does not access private account balances or place live orders.

## Phase 3E Opportunity Intelligence

Run the payout-adjusted review flow:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot ui-summary
kalshi-bot ui
```

The dashboard starts with `Today's Summary`, then a `Paper Portfolio` header, then human-readable trade cards. Each card shows a short market name, category, BUY YES / BUY NO / NO TRADE recommendation, traffic-light label, risk meter, primary driver, supporting signals, and risks. The full Kalshi title is hidden behind `View Market Details`.

`best-payouts` ranks recent opportunities by expected value, payout/risk ratio, and payout-adjusted score while filtering out low-confidence longshots.

## Phase 3F Research Assistant

Run the local research flow:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot research-report --model-name ensemble_v2 --limit 10 --output reports/research_report.md
kalshi-bot ask-research "Why is this ranked #1?" --model-name ensemble_v2
kalshi-bot ui
```

The Research Assistant is deterministic and local. It gathers stored evidence from rankings, forecasts, snapshots, features, model performance, paper positions, paper P&L, fills, backtests, and settlements. It generates analyst-style explanations without external LLM calls or OpenAI API keys.

The UI adds `/research`, `/research/opportunity/{ticker}`, `POST /research/ask`, and `Why?` links on opportunity cards. `research-report` stores `opportunity_research_snapshots`, which support simple change comparisons after repeated runs.

Supported questions include why a market is ranked, why the bot likes it, why it is risky, what data is missing, whether paper/demo review is reasonable, which model is driving it, and how the top 5 opportunities compare.

## Phase 3G Signal Marketplace

Run the local signal flow:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot signal-explorer
kalshi-bot signal-leaderboard
kalshi-bot signal-report --output reports/signal_report.md
kalshi-bot ui
```

Signals are registered in `signals`, extracted deterministically from stored forecasts/rankings/snapshots/features, and attributed through `signal_forecasts` and `signal_trades`. `signal_performance` stores ROI, win rate, P&L, forecast count, trade count, calibration, and confidence.

The UI adds `/signals` and `/signals/{signal_name}`. Opportunity cards show the top three active signal badges, and Research Assistant writeups include signal-aware primary and supporting drivers.

Built-in signals include Weather, Crypto, Economic, Market Divergence, Liquidity, Spread Compression, Momentum, Ensemble Agreement, Opportunity Score, and Fresh Data.

## Phase 3H News Intelligence

Run the local news flow:

```bash
kalshi-bot ingest-news --source rss
kalshi-bot link-news-markets
kalshi-bot build-news-features --window-minutes 360
kalshi-bot forecast --model news_v1
kalshi-bot news-report --output reports/news_report.md
kalshi-bot news-opportunities --model-name news_v1 --limit 20 --output reports/news_opportunities.md
kalshi-bot signal-report --output reports/signal_report.md
kalshi-bot ui
```

For manual import:

```bash
kalshi-bot ingest-news --input-file data/news_sample.json
kalshi-bot ingest-news --input-file data/news_sample.csv
```

`NEWS_RSS_FEEDS_JSON` accepts a JSON list of feed objects with `name`, `url`, and optional
`category`. If no feeds are configured, RSS ingestion explains the missing setting and exits
cleanly. Manual import still works.

The `/news` UI page shows latest news, categories, linked markets, news signals, and
news-driven opportunities. `/news/{id}` shows item detail. News signals also appear in the
Signal Marketplace and Research Assistant evidence when generated.

`news_v1` starts from the stored market midpoint and applies a bounded adjustment from
sentiment, importance, freshness, and market wording. It skips markets with no news features.
Everything remains local, deterministic, paper/demo only, and free of external LLM calls.

## Phase 3K Market Microstructure

Run the local microstructure flow:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot build-microstructure-features --lookback-minutes 60
kalshi-bot forecast --model microstructure_v1
kalshi-bot microstructure-report --output reports/microstructure_report.md
kalshi-bot microstructure-opportunities --model-name microstructure_v1 --limit 20 --output reports/microstructure_opportunities.md
kalshi-bot microstructure-backtest --days 30 --output reports/microstructure_backtest.md
kalshi-bot scheduler-plan --profile microstructure-watch
kalshi-bot ui
```

The engine reads stored snapshots and stored orderbook JSON. It detects spread tightening/widening,
liquidity changes, orderbook pressure, price dislocations versus `ensemble_v2`, late moves, and
possible informed-flow patterns. Possible informed flow is a cautious heuristic, not proof.

The UI adds `/microstructure` and `/microstructure/{ticker}`. Opportunity cards show spread trend,
liquidity trend, orderbook pressure, late-move warning, and possible informed-flow warning.

All output remains local, paper/demo only, and free of live-trading behavior.

## Phase 3L Meta Model

Run the local meta model flow:

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

The Meta Model predicts which existing forecast model should be trusted for a market. It uses
local model history, category performance, paper ROI, signal activity, data freshness, liquidity,
spread quality, feature availability, and model disagreement. `meta_model_v1` uses the selected
model probability. `meta_ensemble_v1` blends model probabilities by trust score.

The UI adds `/meta` and `/meta/{ticker}`. Opportunity cards show the trusted model, trust score,
and fallback badge when evidence is thin. Reports are written to `reports/meta_report.md`,
`reports/meta_evaluation.md`, and `reports/meta_opportunities.md`.

All output remains local, deterministic, paper/demo only, and free of live-trading behavior.

## Phase 3J Sports Intelligence

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

The JSON format supports top-level `league`, `teams`, `games`, `team_stats`, `injuries`,
and `odds`. CSV import supports a `record_type` column for `team`, `game`, `team_stat`,
`injury`, and `odds`. Public/free provider scaffolds explain missing sources; paid sports
APIs are not required.

The `/sports` UI page shows games, league counts, links, sports signals, and sports-driven
opportunity context. `/sports/leagues/{league}` filters by league and
`/sports/games/{game_key}` shows a game-level feature/link view.

`sports_v1` and the league models start from the stored market midpoint and apply bounded
adjustments from linked sports features. They skip markets with no sports link or no
feature row. All output remains paper/demo only.

## Phase 2.5 Backtesting

Run all local forecast models, paper trading, and reports:

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot gh4-paper-activation-preflight
kalshi-bot paper-summary --output reports/paper_trading.md
kalshi-bot backtest --model-name market_implied_v1 --strategy paper_v1 --days 30 --output reports/backtest_market_implied_v1.md
kalshi-bot compare-strategies --days 30 --output reports/strategy_comparison.md
```

External JSON can be stored locally:

```bash
kalshi-bot ingest-external --source weather --input-file path.json
kalshi-bot ingest-external --source crypto --input-file path.json
kalshi-bot ingest-external --source economic --input-file path.json
```

## Phase 2.6 Opportunity Scanner

```bash
kalshi-bot find-opportunities --model-name market_implied_v1 --limit 20 --output reports/opportunities.md
kalshi-bot market-rankings --limit 50 --output reports/market_rankings.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

The scanner ranks markets from stored snapshots and forecasts using edge, liquidity, spread, time-to-close, and model-confidence scores. The leaderboard compares local model performance and includes no-data models with notes.

## Phase 2.7 Crypto Forecasting

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

Crypto ingestion uses public no-key endpoints. `crypto_v2` starts from the stored market midpoint and applies a bounded momentum adjustment only for linked crypto markets with sufficient features.

## Phase 2.8 Weather Forecasting

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

Weather ingestion uses the public NOAA/NWS API with the configured user agent. `weather_v2` starts from the stored market midpoint and applies a bounded adjustment from linked weather features.

## Phase 2.9 Model Tournament

```bash
kalshi-bot tournament --days 30 --output reports/model_tournament.md
kalshi-bot model-diagnostics --days 30 --output reports/model_diagnostics.md
kalshi-bot model-weights --days 30 --output reports/model_weights.md
kalshi-bot forecast --model ensemble_v2
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md
```

The tournament compares `market_implied_v1`, `crypto_v2`, `weather_v2`, `economic_v1`, `ensemble_v1`, and `ensemble_v2` by category. It marks sparse rows as `INSUFFICIENT_DATA`, generates diagnostics, and stores category weights used by `ensemble_v2`.

## Phase 3A Decision UI

```bash
kalshi-bot ui
```

Open `http://127.0.0.1:8080`. The UI displays top opportunities, market detail, risk checks, paper position context, report links, and demo-only execution review controls. Execution controls remain disabled unless `EXECUTION_ENABLED=true`, `EXECUTION_DRY_RUN=false`, the kill switch is off, and all risk checks pass.

## Phase 3B Demo Autopilot

```bash
kalshi-bot autopilot-status
kalshi-bot autopilot-once
kalshi-bot autopilot-report --output reports/autopilot_report.md
kalshi-bot ui
```

The autopilot records local runs, cycles, and risk events. It is disabled by default, requires `KALSHI_ENV=demo`, and uses dry-run mode by default. The UI `/autopilot` page can run one dry-run cycle and link to the generated report. There is no live trading button.

## Phase 3C Decision Cockpit

```bash
kalshi-bot explain-opportunity --ticker TICKER --model-name ensemble_v2
kalshi-bot ui
```

The UI now shows opportunity cards with plain-English recommendations, confidence, edge in cents, score out of 100, top reason, top risk, paper position, demo status, data freshness, and recommended action. The detail page uses Forecast, Market Quality, Risk Checks, Paper History, Backtest History, and Raw Data tabs. Raw JSON is collapsed under Advanced / Raw Data by default.

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

## Safety Note

Phase 1, Phase 2, Phase 2.5, Phase 2.6, Phase 2.7, Phase 2.8, Phase 2.9, Phase 3A, Phase 3B, Phase 3C, Phase 3C.5, Phase 3D, Phase 3E, Phase 3F, Phase 3F-1, Phase 3G, Phase 3H, Phase 3J, Phase 3K, and Phase 3L are read-only, paper-only, or demo-only against Kalshi. This repository contains no code for production live trading, real-money order placement, authenticated production API calls, account access, private keys, signing, balances, or live portfolio management. Paper orders, backtest trades, rankings, opportunities, crypto forecasts, weather forecasts, news forecasts, sports forecasts, microstructure forecasts, meta forecasts, tournament results, generated weights, model confidence scores, learning targets, UI execution reviews, autopilot runs, autopilot cycles, risk events, explanations, research notes, research questions, research snapshots, signal events, signal attribution, signal performance rows, news signals, sports signals, microstructure signals, meta signals, meta decisions, meta training examples, meta performance rows, overnight metrics, workstation snapshots, watchlists, alerts, payout scores, and trade cards are local simulation/diagnostic records only.
