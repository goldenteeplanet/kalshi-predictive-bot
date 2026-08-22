# Canonical System Lanes

Generated: `2026-08-22T00:05:04.830037+00:00`
Status: `READY`

## GH2_OPERATIONAL_SOAK

Prove bounded operational health and safety.

Writes: Health, stage, source-readiness, soak, and invariant evidence only.

Owned resources: command=8, table=9, artifact=4, writer=1, counter=2

Prohibitions: research expansion, paper order creation, shared training counters

### Writers

- `fixed-rate:health-and-soak-stages` — Health artifact writer under the systemd controller.

### Artifacts

- `reports/fixed_rate_health_status.json` — One scheduler-cycle health state.
- `reports/phase_gh2/gh2_active_candidate_refresh.json` — Bounded GH-2 decision evidence.
- `reports/phase3bc_r5/phase3bc_r5_status.json` — Operational freshness-watch status.
- `reports/paper_activation/invariant_status.json` — Read-only invariant assessment for authorized paper orders.

### Counters

- `gh2_healthy_cycles` — Completed healthy fixed-rate cycles; research runs never increment it.
- `gh2_critical_stage_timeouts` — Critical timeouts per operational cycle.

## CURRENT_MARKET_RESEARCH

Scan current markets and produce forecasts, rankings, and shadow evidence.

Writes: Current catalog, source, link, feature, quote, forecast, ranking, and shadow data.

Owned resources: command=337, table=55, artifact=6, writer=3, counter=4

Prohibitions: paper orders, historical-label leakage, GH-2 soak counters

### Writers

- `fixed-rate:active-crypto-and-weather-refresh` — Current catalog/source/feature/forecast/ranking stages.
- `crypto-event-quote-collector` — Current coherent capture and telemetry writer.
- `shadow-signal-and-ledger-scripts` — Artifact-only shadow research writer.

### Artifacts

- `reports/phase3bc_r3/phase3bc_r3_active_crypto_refresh.json` — Current crypto refresh universe and forecast evidence.
- `reports/crypto_event_vectors/status.json` — Coherent current-event quote captures.
- `reports/crypto_event_vectors/forecast_polytope_alignment.json` — Current forecast-to-quote-polytope alignment.
- `reports/crypto_event_vectors/targeted_forecast_telemetry.json` — Targeted current-forecast yield telemetry.
- `reports/overnight_alpha_factory/shadow_signals.jsonl` — Current shadow signals; never paper orders.
- `reports/overnight_alpha_factory/shadow_trades.jsonl` — Current simulated shadow ledger.

### Counters

- `current_supported_markets` — Distinct active semantically supported tickers.
- `current_forecasted_markets` — Distinct current tickers with point-in-time forecasts.
- `current_positive_net_ev` — Current fee-adjusted executable candidates with net EV above zero.
- `shadow_decisions` — Artifact-ledger shadow decisions; excludes paper orders.

## HISTORICAL_REPLAY

Produce no-lookahead historical and settled model evaluations.

Writes: Replay, backtest, settlement, calibration, memory, and evaluation evidence.

Owned resources: command=51, table=53, artifact=3, writer=2, counter=3

Prohibitions: current paper orders, post-outcome features, GH-2 soak counters

### Writers

- `settlement-refresh-and-replay` — Canonical external outcomes and evaluated-history writer.
- `backtest-and-walk-forward-commands` — No-lookahead replay/evaluation writer.

### Artifacts

- `reports/crypto_event_vectors/multiclass_interval_scoring.json` — Settled no-lookahead multiclass evaluation.
- `reports/phase3aa` — Settlement realization and reconciliation evidence.
- `reports/paper_settlement_reconciliation` — Canonical matching diagnostics; paper lane is read-only consumer.

### Counters

- `replay_evaluated_contracts` — No-lookahead evaluated contract forecasts.
- `replay_independent_events` — Distinct terminal events weighted once.
- `settled_shadow_decisions` — Shadow decisions joined to canonical settlements.

## GUARDED_PAPER_LEARNING

Create and evaluate explicitly authorized simulated paper positions.

Writes: Paper orders, fills, positions, P&L, sizing, risk, and paper-learning evidence.

Owned resources: command=36, table=25, artifact=3, writer=2, counter=4

Prohibitions: live execution, unapproved paper creation, shadow-as-paper

### Writers

- `operator-approved-paper-activation` — Only authorized paper-order creation path.
- `phase3m-phase3n-paper-transaction` — Sizing and risk records for guarded paper decisions.

### Artifacts

- `reports/phase3ba_r3/weather_paper_gate.json` — Paper eligibility gate; not authorization.
- `reports/phase3ba_r3/scoped_weather_depth_preflight.json` — Fresh sizing/risk preflight evidence.
- `reports/paper_activation/governance_preflight.json` — Operator approval and guarded activation preflight evidence.

### Counters

- `guarded_paper_orders` — Rows in paper_orders; excludes shadow trades.
- `guarded_paper_fills` — Rows in paper_fills attributable to guarded paper orders.
- `settled_paper_trades` — Filled paper orders with attributable canonical settlement.
- `evaluated_paper_trades` — Settled paper trades with realized model/P&L evaluation.

## Table ownership

- **GH2_OPERATIONAL_SOAK:** `live_readiness_certificate`, `live_readiness_certificate_event`, `readiness_control_result`, `readiness_decision`, `readiness_evidence_manifest`, `readiness_review`, `runtime_provenance_events`, `system_certification_artifact`, `system_certification_run`
- **CURRENT_MARKET_RESEARCH:** `alert_events`, `alerts`, `crypto_current_events`, `crypto_event_liquidity_coverage`, `crypto_event_quote_captures`, `crypto_features`, `crypto_market_links`, `crypto_prices`, `economic_events`, `economic_features`, `economic_market_links`, `feature_snapshots`, `features`, `forecast_skip_log`, `forecasts`, `forum_consensus_signals`, `market_legs`, `market_opportunities`, `market_rankings`, `market_snapshots`, `markets`, `meta_model_decisions`, `meta_model_features`, `microstructure_events`, `microstructure_features`, `microstructure_signals`, `news_features`, `news_items`, `news_market_links`, `news_signals`, `opportunity_research_snapshots`, `orderbook_depth_snapshots`, `personal_trader_recommendation_memory`, `research_notes`, `research_questions`, `signal_events`, `signal_forecasts`, `signal_performance`, `signal_skip_log`, `signal_trades`, `signals`, `sports_features`, `sports_games`, `sports_injuries`, `sports_market_links`, `sports_odds`, `sports_signals`, `sports_team_stats`, `sports_teams`, `watchlist_markets`, `watchlists`, `weather_features`, `weather_forecasts`, `weather_market_links`, `weather_observations`
- **HISTORICAL_REPLAY:** `backtest_runs`, `backtest_trades`, `feature_candidate`, `feature_discovery_run`, `feature_evaluation`, `feature_fold_result`, `feature_holdout_access`, `feature_recommendation`, `feature_relationship`, `feature_segment_result`, `forecast_memory`, `market_memory`, `memory_archive_manifests`, `memory_event_quarantine`, `meta_model_performance`, `meta_model_training_examples`, `model_confidence_scores`, `model_diagnostics`, `model_iteration_metrics`, `model_leaderboard`, `model_tournament_results`, `model_tournament_runs`, `model_weights`, `rl_behavior_decision`, `rl_behavior_policy`, `rl_dataset_manifest`, `rl_drift_snapshot`, `rl_holdout_access_log`, `rl_policy_artifact`, `rl_policy_decision`, `rl_policy_evaluation`, `rl_policy_promotion`, `rl_policy_rollback`, `rl_policy_segment_metric`, `rl_reward_definition`, `rl_reward_ledger`, `rl_run`, `self_evaluation_findings`, `self_evaluation_journals`, `self_evaluation_metrics`, `self_evaluation_runs`, `settlements`, `synthetic_calibration_result`, `synthetic_constraint_result`, `synthetic_contract_registry`, `synthetic_event_registry`, `synthetic_listing_check`, `synthetic_listing_match`, `synthetic_market_run`, `synthetic_model_component`, `synthetic_probability_estimate`, `synthetic_resolution`, `trade_memory`
- **GUARDED_PAPER_LEARNING:** `advanced_risk_decisions`, `advanced_risk_high_water_marks`, `advanced_risk_reservations`, `autopilot_cycles`, `autopilot_metrics`, `autopilot_opportunities`, `autopilot_paper_trades`, `autopilot_runs`, `learning_cycles`, `learning_metrics`, `learning_opportunities`, `learning_paper_trades`, `learning_rejection_log`, `learning_runs`, `learning_trade_targets`, `overnight_cycles`, `overnight_runs`, `paper_fills`, `paper_orders`, `paper_pnl`, `paper_positions`, `portfolio_snapshots`, `position_history`, `position_sizing_decisions`, `risk_events`

## Command ownership

- **GH2_OPERATIONAL_SOAK:** `gh2-single-writer-decision-refresh`, `gh2-stage-crypto-quotes`, `live-readiness-guard-check`, `live-readiness-review`, `live-readiness-status`, `system-certification-report`, `system-certification-run`, `system-certification-status`
- **CURRENT_MARKET_RESEARCH:** `active-crypto-router`, `active-universe-doctor`, `analytics-report`, `ask-research`, `best-payouts`, `build-crypto-features`, `build-economic-features`, `build-meta-features`, `build-meta-training`, `build-microstructure-features`, `build-news-features`, `build-sports-features`, `build-weather-features`, `candidate-coverage-audit`, `candidate-funnel-audit`, `catalog-lineage-repair`, `category-coverage-gap-audit`, `collect-once`, `command-audit`, `compare-strategies`, `control-center-report`, `crypto-forecast-doctor`, `crypto-history-warmup`, `crypto-report`, `crypto-watch-status`, `crypto-window-sync`, `daily-briefing`, `db-doctor`, `db-health`, `db-locks`, `db-migrate`, `db-revision`, `db-writer-monitor`, `derive-sports-schedule`, `economic-news-market-watch`, `explain-opportunity`, `find-opportunities`, `forecast`, `forecast-signals`, `gap-closure-doctor`, `gh1-websocket-orderbook`, `gh1-websocket-orderbook-drain`, `gh1-websocket-orderbook-watch`, `gh1d-liquidity-truth`, `gh1e-discover-quoted`, `gh1f-demo-quote-monitor`, `gh1g-liquidity-census`, `gh1j-ranking-wiring-audit`, `gh1k-liquidity-repair-preview`, `gh1l-guarded-activation`, `gh1m-opportunity-attribution`, `gh1n-timing-audit`, `gh1o-independent-edge-discovery`, `gh1p-current-window-refresh`, `gh1q-forecast-skip-attribution`, `ingest-crypto`, `ingest-economic`, `ingest-external`, `ingest-forum-consensus`, `ingest-news`, `ingest-sports`, `ingest-weather`, `init-db`, `institutional-dashboard-export`, `institutional-dashboard-report`, `institutional-dashboard-status`, `leaderboard`, `link-coverage`, `link-crypto-markets`, `link-economic-markets`, `link-news-markets`, `link-remediate`, `link-sports-markets`, `link-weather-markets`, `long-job-monitor`, `market-coverage-doctor`, `market-data-refresh`, `market-legs-parse`, `market-rankings`, `meta-evaluate`, `meta-opportunities`, `meta-report`, `microstructure-opportunities`, `microstructure-report`, `microstructure-sample-watchlist`, `migrate-sqlite-to-postgres`, `model-confidence`, `model-feature-repair`, `model-health`, `model-link-repair`, `model-metrics-reconcile`, `model-readiness`, `model-readiness-report`, `model-repair-audit`, `model-repair-run`, `models-status`, `news-opportunities`, `news-report`, `nyc-w3-live-alignment-preview`, `nyc-w4-observation-feature-preview`, `nyc-w8-live-shadow-drift-certification`, `opportunity-link-audit`, `overnight-once`, `overnight-report`, `overnight-run`, `overnight-status`, `personal-trader-audit`, `personal-trader-brief`, `personal-trader-status`, `phase-3aj-report`, `phase-3ak-report`, `phase-3al-diagnostic`, `phase-orchestrator`, `phase-status`, `phase3ac-sports-provenance-repair`, `phase3ae-fast-market-harvester`, `phase3ae-roster-candidate-diagnostics`, `phase3ae-verified-sports-connector`, `phase3af-sports-schedule-bootstrap`, `phase3ag-crypto-pipeline`, `phase3ag-sports-ambiguity-coverage`, `phase3ag-sports-link-repair-pass`, `phase3ah-r2-player-prop-backfill`, `phase3ah-r3-bounded-scan-expansion`, `phase3ah-r3-sports-provenance-repair`, `phase3ah-roster-participant-verification`, `phase3ah-round-placeholder-resolution`, `phase3ah-schedule-roster-evidence`, `phase3ah-sports-evidence-backfill`, `phase3ah-sports-placeholder-watch`, `phase3ai-link-reconciliation`, `phase3aj-sports-alias-provenance`, `phase3ak-multileg-provenance`, `phase3am-gap-burndown`, `phase3am-preflight`, `phase3am-sports-verified-upgrade`, `phase3an-3bb-r2-burndown`, `phase3an-crypto-feature-completeness`, `phase3an-crypto-watch-doctor`, `phase3an-crypto-watch-restart-plan`, `phase3an-economic-approval-safety-guard`, `phase3an-economic-link-event-repair`, `phase3an-economic-link-event-repair-plan`, `phase3an-economic-morning-operator-handoff`, `phase3an-economic-news-parser-backfill-plan`, `phase3an-economic-news-watch`, `phase3an-economic-operator-approval-packet`, `phase3an-economic-parser-leg-backfill`, `phase3an-gap-fix-report`, `phase3an-general-sources-status`, `phase3an-overnight-refresh-continuity`, `phase3an-preflight`, `phase3an-sports-blocker-report`, `phase3an-usda-date-mismatch-report`, `phase3ap-book-diagnostic`, `phase3ap-night-runner-v2`, `phase3ap-refresh-positive-ev-books`, `phase3aq-link-and-book-unblock-report`, `phase3aq-positive-ev-link-audit`, `phase3aq-refresh-verified-opportunity-books`, `phase3aq-self-improvement`, `phase3ar-catalog-stale-diagnostic`, `phase3ar-crypto-forecast-coverage`, `phase3ar-link-repair-report`, `phase3ar-refresh-books-for-verified-links`, `phase3ar-refresh-catalog-for-opportunities`, `phase3as-active-universe`, `phase3at-active-router`, `phase3at-forecast-ranking-diagnostic`, `phase3at-handoff-report`, `phase3at-opportunity-funnel`, `phase3au-report`, `phase3au-status`, `phase3aw-crash-report`, `phase3aw-dashboard-truth`, `phase3aw-status`, `phase3ax-gap-analysis`, `phase3ax-r9-guarded-refresh-job`, `phase3ax-sports-derivation`, `phase3ay-category-readiness`, `phase3ay-free-source-adapter-registry`, `phase3ay-free-source-market-scan`, `phase3ay-free-source-sprint-report`, `phase3ay-health-refresh`, `phase3ay-positive-ev-accelerator`, `phase3ay-status`, `phase3ay-unattended-guard`, `phase3ay-unattended-start`, `phase3az-gap-analysis`, `phase3az-r11-non-crypto-category-activation`, `phase3az-r12-weather-activation-preview`, `phase3az-r12-weather-missing-link-apply`, `phase3az-r13-weather-handoff-status`, `phase3ba-ingestion-stability-report`, `phase3ba-r1-writer-unlock`, `phase3ba-r2-weather-ranking-activation`, `phase3ba-r4-crypto-executable-book-watch`, `phase3ba-r6-noncrypto-engine-backlog`, `phase3ba-r7-composite-market-plan`, `phase3ba-status`, `phase3bb-acceleration-report`, `phase3bb-cloud-readiness`, `phase3bb-domain-readiness`, `phase3bb-multicategory-expansion-plan`, `phase3bb-r1-operator-scheduler`, `phase3bb-r10-cloud-readiness-decision`, `phase3bb-r11-codex-cloud-bridge`, `phase3bb-r12-cloud-bootstrap-verification`, `phase3bb-r13-cloud-scheduler-adoption`, `phase3bb-r14-cloud-service-plan`, `phase3bb-r15-cloud-service-install-review`, `phase3bb-r16-cloud-service-install-handoff`, `phase3bb-r17-cloud-service-install-verification`, `phase3bb-r18-cloud-scheduler-runtime-cutover`, `phase3bb-r19-cloud-systemd-cutover`, `phase3bb-r2-apply-group-source-review`, `phase3bb-r2-general-candidate-routing`, `phase3bb-r2-general-source-availability`, `phase3bb-r2-general-source-evidence`, `phase3bb-r2-general-source-intake`, `phase3bb-r2-group-source-review`, `phase3bb-r2-weather-fast-lane`, `phase3bb-r20-cloud-ui-service-plan`, `phase3bb-r21-cloud-ui-install-review`, `phase3bb-r22-cloud-ui-install-handoff`, `phase3bb-r23-cloud-ui-install-verification`, `phase3bb-r24-cloud-ui-start-tunnel-verification`, `phase3bb-r25-cloud-ui-operator-smoke-test`, `phase3bb-r26-cloud-ui-access-control-gate`, `phase3bb-r27-cloud-ui-private-access-auth-draft`, `phase3bb-r28-cloud-ui-private-access-operator-review`, `phase3bb-r29-cloud-ui-private-access-install-handoff`, `phase3bb-r3-composite-operator-preflight`, `phase3bb-r3-composite-preview-gate`, `phase3bb-r3-exact-sports-link`, `phase3bb-r3-free-source-inventory`, `phase3bb-r3-general-reclassification`, `phase3bb-r3-safe-parser-reparse`, `phase3bb-r3-source-evidence-activation`, `phase3bb-r30-cloud-ui-private-access-install-verification`, `phase3bb-r31-cloud-ui-private-access-operator-smoke-test`, `phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification`, `phase3bb-r34-cloud-multicategory-refresh-scheduler-review`, `phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run`, `phase3bb-r36-cloud-scheduler-install-handoff`, `phase3bb-r37-cloud-scheduler-install-verification`, `phase3bb-r38-cloud-scheduler-install-repair-handoff`, `phase3bb-r38-cloud-scheduler-timer-start-handoff`, `phase3bb-r39-cloud-auto-login-admin-bootstrap`, `phase3bb-r4-economic-parser-backfill`, `phase3bb-r4-flightaware-review-link-gate`, `phase3bb-r40-cloud-scheduler-runtime-monitor`, `phase3bb-r41-writer-gate-normalization`, `phase3bb-r42-weather-fast-lane-post-unblock-verification`, `phase3bb-r43-single-writer-coordinator`, `phase3bb-r43-weather-catalog-scheduler-hook`, `phase3bb-r44-weather-catalog-hook-runtime-verification`, `phase3bb-r45-weather-freshness-to-ranking-impact`, `phase3bb-r46-cloud-scheduler-weather-writer-gate-repair`, `phase3bb-r47-weather-current-window-series-discovery-linkability-repair`, `phase3bb-r48-weather-feature-refresh-runtime-verification`, `phase3bb-r49-weather-missing-link-apply-after-feature-refresh`, `phase3bb-r5-flightaware-date-stable-evidence`, `phase3bb-r5-usda-source-activation`, `phase3bb-r50-weather-post-link-ranking-fast-lane-recheck`, `phase3bb-r51-weather-ranking-path-repair`, `phase3bb-r52-weather-ev-fair-value-diagnostic`, `phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair`, `phase3bb-r54-weather-missing-link-apply-deferral`, `phase3bb-r55-weather-ranking-path-retry`, `phase3bb-r57-weather-selected-window-pipeline-speed-repair`, `phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair`, `phase3bb-r59-weather-catalog-refresh-r57-retry`, `phase3bb-r6-sports-provenance-repair`, `phase3bb-r60-weather-next-window-lead-time-scheduler-repair`, `phase3bb-r61-cloud-dashboard-db-writer-api-reachability-repair`, `phase3bb-r7-news-event-discovery`, `phase3bb-scheduler-plan`, `phase3bb-throughput-analysis`, `phase3bb-weather-fast-lane`, `phase3bb-workspace-guard`, `phase3bc-crypto-clean-opportunity-router`, `phase3bc-r17-crypto-liquidity-actionability`, `phase3bc-r3-active-crypto-refresh`, `phase3bc-r4-crypto-ev-risk-diagnostics`, `phase3bc-r5-crypto-freshness-watch`, `phase3bc-r5-status`, `phase3bc-r5-unattended-guard`, `phase3bc-r5-unattended-start`, `phase3bc-r7-crypto-ranking-coverage-repair`, `phase3bd-economic-market-discovery`, `phase3bd-r2-economic-calendar-freshness`, `phase3bd-r3-economic-value-capture`, `phase3bd-r4-verified-consensus-source`, `phase3bd-r5-consensus-feed-watch`, `phase3bd-r7-economic-opportunity-quality-gate`, `phase3bd-r8-economic-evidence-activation`, `phase3x-audit`, `phase3x-report`, `phase3x-status`, `phase3y-report`, `phase3z-r2-sports-provenance-repair`, `pmb-regression-benchmark`, `portfolio-summary`, `research-opportunity`, `research-report`, `roadmap-runtime-reports`, `runtime-identity`, `runtime-origin`, `scheduler-plan`, `signal-explorer`, `signal-leaderboard`, `signal-performance`, `signal-report`, `signals-report`, `signals-status`, `snapshot`, `snapshot-coverage-repair`, `source-readiness-report`, `sports-link-cleanup`, `sports-opportunities`, `sports-report`, `sqlite-backup`, `sqlite-recover`, `sync-markets`, `system-lanes`, `system-remediate`, `system-remediation-report`, `tonight-check`, `tonight-report`, `tonight-run`, `ui`, `ui-shell-status-refresh`, `ui-summary`, `weather-identity-evidence-shadow`, `weather-report`, `workspace-guard`
- **HISTORICAL_REPLAY:** `backtest`, `composite-settlement-resolve`, `crypto-backtest`, `feature-discovery-report`, `feature-discovery-run`, `feature-discovery-status`, `feature-experiment-export`, `gh1h-production-liquidity-calibration`, `gh1i-two-sided-calibration`, `memory-archive`, `memory-backfill`, `memory-dataset`, `memory-report`, `memory-status`, `memory-timeline`, `microstructure-backtest`, `model-diagnostics`, `model-weights`, `news-backtest`, `nyc-w5-multi-window-shadow-calibration`, `phase3aa-r2-exact-settlement-harvest`, `phase3aa-r3-residual-settlement-audit`, `phase3aa-r4-settlement-fetch-recovery`, `phase3aa-r5-closed-market-outcome-capture`, `phase3aa-r6-composite-settlement-resolver`, `phase3aa-realize`, `phase3an-settlement-health-confirm`, `phase3ap-settlement-check-diagnostic`, `phase3aq-settlement-check-split`, `phase3ar-settlement-check-noise-audit`, `phase3ar-url-audit`, `phase3ar-url-repair`, `phase3ay-due-settlement-diagnostic`, `phase3bb-historical-replay-acceleration`, `report-calibration`, `rl-dataset`, `rl-drift-report`, `rl-evaluate`, `rl-shadow-report`, `rl-status`, `rl-train`, `self-evaluate`, `settlement-watch`, `sports-backtest`, `sync-settlements`, `synthetic-markets-report`, `synthetic-markets-run`, `synthetic-markets-status`, `tournament`, `trade-memory-timeline`, `weather-backtest`
- **GUARDED_PAPER_LEARNING:** `accelerate-learning`, `advanced-risk-report`, `autopilot-once`, `autopilot-report`, `autopilot-run`, `autopilot-status`, `gh4-paper-activation-preflight`, `learning-diagnostics`, `learning-once`, `learning-report`, `learning-run`, `learning-status`, `learning-targets`, `paper-pnl`, `paper-reset`, `paper-run`, `paper-settlement-doctor`, `paper-summary`, `paper-trade-funnel`, `paper-trading-gap-analysis`, `phase3-overnight-exploratory-paper-seed`, `phase3ab-learning-governor`, `phase3al-learning-resume`, `phase3an-paper-funnel-explain`, `phase3ao-learning-reward-pipeline`, `phase3ap-paper-ready-unblock-report`, `phase3ay-multicategory-paper-funnel`, `phase3ay-settle-due-paper`, `phase3ba-paper-certification`, `phase3ba-r3-weather-paper-gate`, `phase3ba-r5-paper-ready-truth`, `phase3bb-r33-cloud-paper-only-operations-readiness`, `phase3bb-r8-unified-paper-gate`, `phase3bb-r9-learning-acceleration`, `phase3bc-r16-crypto-paper-ready-edge-hunt`, `weather-one-contract-paper-activation`

## Shared read contracts

- All lanes read the same semantic market definitions.
- All lanes read the same model, feature, EV, and fee definitions.
- Historical Replay owns canonical settlements; Guarded Paper consumes them read-only.
- GH-2 consumes current-market evidence but never increments research or paper counters.

## Physical-boundary findings

- `forecasts` → **CURRENT_MARKET_RESEARCH**: Historical reconstruction writes backtest/replay evidence, not current forecasts.
- `settlements` → **HISTORICAL_REPLAY**: Paper P&L may read outcomes but may not fabricate or rewrite them.
- `fixed-rate-service` → **GH2_OPERATIONAL_SOAK**: It is a controller; each child writer is charged to its declared lane.

## Consolidation gate

No wrapper may be deleted or redirected until its command and every artifact/table it writes resolve to the same lane in this contract. Shadow artifacts remain distinct from guarded paper tables.
