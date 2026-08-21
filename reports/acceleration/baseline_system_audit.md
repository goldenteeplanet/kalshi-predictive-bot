# Kalshi Bot Phase 0 Baseline System Audit

Generated: 2026-08-21T20:11:46Z  
Behavioral changes during audit: none  
Database access: SQLite URI `mode=ro` plus `PRAGMA query_only=ON`

## Executive baseline

The application has substantial working crypto, weather, settlement, guarded-paper, and reporting infrastructure. The strongest current production evidence is fresh specialist forecasting: `crypto_v2` produced 5,197 forecasts and `weather_v2` produced 1,964 forecasts in the preceding 24 hours. The database contains 529,126 forecasts, 227,803 rankings, 1,526 stored opportunities, 326,993 settlement rows, 204 paper orders, and 204 fills.

The primary operational blocker is scheduler continuity, not missing forecasting code. The last complete guarded canary (`1787336818`) was healthy across 17 stages and preserved safety counts `204 / 239 / 239`, but the next persistent cycle (`1787337421`) was externally stopped at 13:41:39 CDT while entering GH-2. At this audit point the service is enabled but inactive. The UI systemd unit is active, but HTTP `/` did not return within three seconds.

There is also evidence of an additional writer outside that fixed-rate service: `crypto_prices`, `crypto_features`, `market_rankings`, and `signal_events` advanced to approximately 19:59Z after the fixed-rate service stopped at 18:41Z. Its provenance must be identified before the scheduler is treated as the sole canonical runtime.

The canonical settlement report identifies 203 settled paper trades and one open paper trade (order 204). Order 204 remains healthy, exactly attributed to Phase 3M/3N IDs `231 / 231`, and awaits settlement. The database-level query that merely sees a non-null `paper_pnl.realized_pnl` reports 204; that is not the canonical evaluated-trade definition because the open position has mark-to-market rows. Phase 21 reconciliation must remain authoritative.

## Repository

| Field | Value |
|---|---|
| Development Git root | `/home/james/kalshi-main-baseline` |
| Branch | `fix/shadow-preflight-isolation-20260821` |
| HEAD | `b35828cad2c81d83231f7d97d9f569a498e867c6` |
| Base `main` / `origin/main` | `cc3aca4ef7194ddf1665beea93b4a68d8425e74b` |
| Ahead / behind `main` | 2 / 0 |
| Dirty state | clean |
| Runtime root | `/home/james/kalshi-runtime-src` |
| Runtime Git metadata | absent by design; deployed source tree |
| Python | 3.12.3 |
| Virtualenv | `/home/james/kalshi-runtime-src/.venv` |
| Imported package | `/home/james/kalshi-runtime-src/src/kalshi_predictor/__init__.py` |
| Persistent DB | `/home/james/kalshi-predictive-bot-data/kalshi_phase1.db` |
| DB size at audit | 23,230,451,712 bytes |
| Runtime data link | `/mnt/d/KalshiPredictiveBotStorage/windows-kalshi-predictive-bot/data` |
| Report root | `/home/james/kalshi-runtime-src/reports` |

The requested `~/projects/kalshi-predictive-bot` checkout is not authoritative on this host. Development, runtime source, persistent database, and report roots are deliberately separated, but runtime-origin metadata is not yet exposed through a single canonical command.

## Safety state

| Control | Observed value |
|---|---:|
| `execution_enabled` | `false` |
| `execution_dry_run` | `true` |
| `paper_order_creation_enabled` | `false` |
| `paper_order_kill_switch` | `true` |
| Paper orders | 204 |
| Position-sizing decisions | 239 |
| Advanced-risk decisions | 239 |
| Order 204 invariant | `HEALTHY` |
| Order 204 settlement | `AWAITING_SETTLEMENT` |
| Order 204 order/fill count | 1 / 1 |
| Order 204 Phase 3M/3N attribution | 231 / 231 |

The scoped weather preflight isolation fix was validated by a real scheduler canary: the preflight completed while all three guarded counts remained `204 / 239 / 239`.

## Runtime

| Item | Baseline |
|---|---|
| Fixed-rate service | enabled, inactive |
| Last successful completion | 2026-08-21T18:36:30.387292Z |
| Last complete cycle | `1787336818`, `HEALTHY`, 17/17 stages complete |
| Interrupted cycle | `1787337421`, stopped externally at 2026-08-21T18:41:39Z |
| Artifact state after interruption | `RUNNING`, current stage `coinbase_stage` (stale/incomplete cycle artifact) |
| Consecutive runtime healthy cycles | 4 / 24 in fixed-rate artifact |
| Fast weather preflight soak | complete; 5 consecutive healthy cycles, 3 required |
| Writer lock | no holder observed at audit |
| UI service | active |
| UI HTTP probe | timeout after 3.002 seconds, zero bytes |
| Kalshi transport | bounded REST scheduler; WebSocket explicitly not applicable |
| Coinbase in interrupted artifact | healthy, 5 symbols, 0 reported errors |
| NOAA in interrupted artifact | pending because cycle stopped before final classification |

### Observed stage runtimes in interrupted cycle

| Stage | Approx. duration | Result |
|---|---:|---|
| Active crypto snapshot/forecast | 54s | complete |
| Active crypto ranking finalize | 62s | complete |
| Early UI shell refresh | 19s | complete |
| Weather catalog refresh | 25s | complete |
| Supported weather preparation | 56s | complete |
| CLIAUS historical monthly harvest | 3s | complete |
| CLIAUS monthly rain preparation | 3s | complete |
| Supported weather snapshot/forecast | 41s | complete; 17/17 snapshots, forecasts, rankings |
| Coinbase stage | 11s | complete |
| GH-2 | began, then external service stop | incomplete |

No timeout reason was recorded. The failure mode is an external stop/controller action, not a critical-stage timeout.

## Database inventory

Critical-null counts below cover applicable `ticker`, `model_name`, `forecast_id`, capture/forecast timestamp, and settlement-result fields. Zero is omitted unless material.

| Table | Rows | Earliest | Latest | Critical nulls / note |
|---|---:|---|---|---|
| markets | 666,745 | n/a | n/a | result 335,916; ticker 0 |
| market_snapshots | 425,982 | 2026-06-24 03:16:58 | 2026-08-21 18:40:52 | 0 |
| feature_snapshots | 364,670 | 2026-06-24 03:16:58 | 2026-08-21 18:40:52 | 0 |
| features | 0 | n/a | n/a | Generic table unused; specialist feature tables are populated |
| forecasts | 529,126 | 2026-06-24 03:16:58 | 2026-08-21 18:40:52 | 0 |
| forecast_memory | 896,464 | 2026-06-24 03:16:58 | 2026-08-21 18:40:52 | forecast_id 0 |
| forecast_skip_log | 78,550 | n/a | n/a | 0 |
| market_rankings | 227,803 | 2026-06-24 03:23:48 | 2026-08-21 19:59:32 | 0 |
| market_opportunities | 1,526 | n/a | n/a | 0 |
| signal_events | 1,342,460 | 2026-06-24 03:17:13 | 2026-08-21 19:59:32 | 0 |
| signals | 39 | 2026-06-24 03:17:13 | 2026-06-24 03:17:13 | n/a |
| signal_trades | 615 | 2026-06-24 03:23:50 | 2026-08-21 01:41:40 | n/a |
| settlements | 326,993 | 2026-06-24 03:23:47 | 2026-08-21 18:35:57 | result 1 (open order 204 market) |
| paper_orders | 204 | 2026-06-24 03:23:49 | 2026-08-21 01:41:40 | 0 |
| paper_fills | 204 | n/a | n/a | 0 |
| paper_positions | 204 | 2026-08-21 01:44:02 | 2026-08-21 18:36:08 | 0 |
| paper_pnl | 59,284 | 2026-06-24 03:23:50 | 2026-08-21 18:36:08 | 0; repeated mark/P&L history, not trade count |
| learning_paper_trades | 203 | 2026-06-24 03:23:49 | 2026-07-06 18:29:41 | 0 |
| autopilot_paper_trades | 0 | n/a | n/a | Tier 2 automation not active |
| position_sizing_decisions | 239 | 2026-06-24 03:23:49 | 2026-08-21 03:24:40 | 0 |
| advanced_risk_decisions | 239 | 2026-06-24 03:23:49 | 2026-08-21 03:24:40 | 0 |
| crypto_prices | 9,746 | 2026-06-24 04:11:29 | 2026-08-21 19:59:13 | n/a |
| crypto_features | 9,740 | 2026-06-24 04:12:07 | 2026-08-21 19:59:15 | n/a |
| crypto_market_links | 70,815 | n/a | n/a | ticker 0 |
| crypto_current_events | 161 | n/a | n/a | n/a |
| crypto_event_quote_captures | 0 | n/a | n/a | Canonical DB table unused; collector evidence is artifact/history based |
| weather_observations | 0 | n/a | n/a | Station observations not stored in this table |
| weather_forecasts | 18,958 | 2026-06-24 05:02:50 | 2026-08-21 18:40:41 | n/a |
| weather_features | 873,057 | 2026-06-24 05:03:43 | 2026-08-21 18:40:41 | n/a |
| weather_market_links | 763 | n/a | n/a | ticker 0 |
| economic_events | 16 | 2026-06-24 05:05:40 | 2026-07-01 19:04:48 | stale |
| economic_features | 40 | 2026-06-24 05:06:20 | 2026-07-01 19:04:48 | stale |
| economic_market_links | 274 | n/a | n/a | ticker 0; current enabled coverage 0 |
| news_items | 3 | 2026-06-17 12:00:00 | 2026-06-17 14:00:00 | fixture/stale evidence |
| news_features | 200 | 2026-06-24 05:09:40 | 2026-06-24 05:09:40 | stale |
| news_market_links | 201 | 2026-06-24 05:09:00 | 2026-06-24 05:09:01 | stale |
| news_signals | 410 | 2026-06-24 05:09:41 | 2026-06-24 05:09:41 | stale |
| sports_games | 51,964 | 2026-06-24 19:27:09 | 2026-07-03 04:02:43 | stale |
| sports_features | 4,993 | 2026-06-24 19:27:09 | 2026-07-03 04:44:13 | stale |
| sports_market_links | 53,513 | 2026-06-24 18:35:49 | 2026-07-10 05:16:59 | stale/current derived links need semantics audit |
| microstructure_events | 2 | 2026-06-29 10:01:23 | same | research-only evidence |
| microstructure_features | 1 | 2026-06-29 10:01:23 | same | not operationally integrated |
| model_confidence_scores | 4,512 | 2026-06-24 03:23:48 | 2026-07-03 18:31:28 | stale |
| model_weights | 4,512 | 2026-06-24 03:23:48 | 2026-07-03 18:31:28 | stale |
| model_leaderboard | 544 | 2026-06-24 05:30:08 | 2026-06-24 16:31:56 | stale |
| backtest_trades | 0 | n/a | n/a | no canonical replay trade records |

## Trading and evaluation evidence

| Evidence | Count | Definition/source |
|---|---:|---|
| Forecasts | 529,126 | `forecasts` |
| Rankings | 227,803 | `market_rankings` |
| Opportunities | 1,526 | `market_opportunities` |
| Signal events | 1,342,460 | event log; not independent decisions |
| Signal trades | 615 | `signal_trades`; semantics need Phase 20 audit |
| Paper orders | 204 | guarded table |
| Paper fills | 204 | guarded table |
| Settled paper trades | 203 | exact-ticker canonical reconciliation |
| Open paper trades | 1 | order 204 |
| Settled forecasts | 2,801 | forecast-to-exact-settlement join |
| Evaluated paper trades | 203 canonical | settlement reconciliation; raw P&L join misleadingly returns 204 |
| Shadow ledger trades | 0 in isolated current ledger | latest shadow artifact reports 0 accepted trades |
| Backtest trade rows | 0 | canonical table |

The dominant opportunity-loss evidence in the latest isolated shadow report is fee/execution adjusted EV: 73,146 observations fail net EV after costs, 73,108 fail executable EV, 72,027 fail usable-book checks, and 50,614 hit the production gate “expected value is not positive at current ask prices.” These counts overlap and are not a first-blocker funnel yet.

## Model inventory

All names below are registered in `forecasting.registry.MODEL_NAMES`. “Invoked” means at least one stored forecast; it does not prove current readiness.

| Model | Registered | Stored forecasts | Last forecast | Last 24h | Baseline classification |
|---|---|---:|---|---:|---|
| market_implied_v1 | yes | 167,243 | 2026-08-14 | 0 | implemented, stale |
| weather_v1 | yes | 0 | none | 0 | registered, not evidenced |
| weather_v2 | yes | 2,742 | 2026-08-21 18:40 | 1,964 | verified active |
| crypto_v1 | yes | 0 | none | 0 | registered, not evidenced |
| crypto_v2 | yes | 314,297 | 2026-08-21 18:37 | 5,197 | verified active |
| economic_v1 | yes | 540 | 2026-07-01 | 0 | implemented, stale/not in current lane |
| news_v1 | yes | 28 | 2026-06-24 | 0 | fixture/stale, not operational |
| mlb_v1 | yes | 33 | 2026-06-28 | 0 | stale |
| nba_v1 | yes | 0 | none | 0 | registered, not evidenced |
| nfl_v1 | yes | 0 | none | 0 | registered, not evidenced |
| nhl_v1 | yes | 0 | none | 0 | registered, not evidenced |
| sports_v1 | yes | 160 | 2026-06-28 | 0 | implemented, stale |
| microstructure_v1 | yes | 3 | 2026-06-29 | 0 | research-only/disconnected |
| meta_model_v1 | yes | 11,000 | 2026-06-29 | 0 | implemented, stale |
| meta_ensemble_v1 | yes | 11,000 | 2026-06-29 | 0 | implemented, stale |
| ensemble_v1 | yes | 11,000 | 2026-06-29 | 0 | implemented, stale |
| ensemble_v2 | yes | 11,100 | 2026-07-11 | 0 | implemented, currently stale; effective-weight audit required |

Model-confidence and leaderboard artifacts have not advanced since July 3 and June 24 respectively. Forecast volume alone must not be interpreted as evidence of edge. Model-specific settled/historical evaluation counts require the canonical replay and reconciliation work in later phases.

## Link and market coverage

The newest deep coverage artifact was generated around 2026-08-21T07:35Z and is therefore historical relative to this audit. It reports a local raw catalog of 655,795 at that cutoff; the database has since grown to 666,745 markets.

| Category | Current parsed | Current linkable | Current linked | Current status | Historical note |
|---|---:|---:|---:|---|---|
| Crypto | 134 | 134 | 134 | connected | 39,822 / 41,772 historical linkable; 1,950 historical unlinked |
| Weather | 98 | 76 | 76 | connected | 22 unsupported composite markets parked |
| Economic | 9 | 0 | 0 | parked composite | no enabled current economic pipeline |
| Sports | 5 | 5 | 5 | derived connected | 51,105 derived markets; independence/semantics risk |
| News | 0 | 0 | 0 | no current markets | stored news evidence is stale/fixture-like |

The last complete current weather preparation found exactly 17 supported tickers and produced 17 snapshots, 17 `weather_v2` forecasts, and 17 rankings, with two opportunities. Current crypto diagnostics had exact links/features/forecasts but many candidates were inside the configured final-entry cutoff.

Crypto distribution research currently has nine coherent/aligned events, eight interval-eligible events, zero settled interval-eligible events, and zero reliable complete-distribution families. Pessimistic market comparison remains correctly blocked until at least ten eligible events settle.

## Baseline answers to the immediate system questions

1. We see a very large historical local catalog (666,745 rows), but do not yet have a canonical fresh full-open-universe count.
2. Current enabled link coverage is strong for bounded crypto and supported weather, but economic/news and composite sports semantics are not operationally complete.
3. Only `crypto_v2` and `weather_v2` show current invocation evidence.
4. Candidate loss is dominated by non-positive fee-adjusted executable EV and unusable books; current reports are overlapping rejection totals, not a canonical first-blocker funnel.
5. Paper volume is intentionally limited by authorization. There are 203 historical settled fills and one authorized open fill; automatic Tier 2 paper ordering remains disabled.
6. Settlement handling is working for 203 historical orders. Order 204 cannot settle before Kalshi publishes the August CLIAUS result.
7. The system does not yet expose trustworthy model-specific edge evidence in one canonical report. Existing confidence/leaderboard data is stale.
8. The largest immediate operational gap is scheduler continuity/external-stop provenance. The largest research gap is a no-lookahead replay/reconciliation lane that turns existing historical data into independent evaluated events.

## Phase 0 stop conditions and blockers

- `STOP`: persistent scheduler continuity is not healthy. An external controller stopped cycle `1787337421` during GH-2, leaving the unified artifact in `RUNNING` state while the service is inactive.
- `STOP`: specialist market data continued advancing after the fixed-rate service stopped, proving another automation/controller can write the production database outside the claimed canonical scheduler.
- `STOP`: UI process health and UI availability disagree: systemd says active, but the HTTP probe timed out.
- `YELLOW`: runtime source has no Git metadata, so deployed SHA cannot be proven from the runtime tree itself.
- `YELLOW`: generic `features` and `weather_observations` tables are empty while specialist artifacts/tables contain data; source definitions are fragmented.
- `YELLOW`: historical link coverage is large but its artifact is stale and mixes raw contracts, derived sports markets, and independent events.
- `YELLOW`: canonical shadow/replay evidence is not consolidated; raw event counts overstate independent training evidence.
- `YELLOW`: model confidence, ensemble weights, leaderboard, economic, news, sports, and microstructure evidence is stale.

Per the acceleration plan’s Phase 43 rules, behavioral acceleration work must not proceed until scheduler-controller provenance and UI reachability are diagnosed. Research-only inventory and report consolidation can continue without writing the production database.

## Evidence sources

- `/tmp/kalshi_phase0_audit.json` (exact read-only database/repository capture)
- `reports/fixed_rate_health_status.json`
- `reports/ui/shell_status_snapshot.json`
- `reports/paper_activation/invariant_status.json`
- `reports/paper_settlement_reconciliation/paper_settlement_reconciliation.json`
- `reports/market_coverage_deep/market_coverage_doctor.json`
- `reports/market_coverage_deep/link_coverage.json`
- `reports/phase3bc_r3/phase3ar/phase3ar_crypto_forecast_coverage.json`
- `reports/crypto_event_vectors/liquidity_coverage_status.json`
- systemd user-unit state and journal for `kalshi-fixed-rate-refresh.service` and `kalshi-ui.service`
