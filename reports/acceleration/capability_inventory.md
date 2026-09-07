# Phase 1 Capability Inventory and Duplication Audit

Generated: 2026-08-21 (America/Chicago)

## Scope and classification rules

This inventory treats `/home/james/kalshi-main-baseline` as the authoritative development
checkout, `/home/james/kalshi-runtime-src` as the deployed runtime copy, and
`/home/james/kalshi-predictive-bot-data/kalshi_phase1.db` as persistent production data.
Runtime evidence means a current artifact, database record, or completed fixed-rate stage;
tests alone do not qualify as runtime evidence.

## Controller and writer reconciliation

| Concern | Finding | Resolution |
|---|---|---|
| Authoritative scheduler | `kalshi-fixed-rate-refresh.service`, owned by WSL user systemd | Retained as the only controller allowed to run refresh/write stages. |
| External controller | Active Codex heartbeat `kalshi-paper-only-soak-monitor` ("Overnight Kalshi alpha factory") could start/stop the service and run diagnostics every 15 minutes | Updated to monitor-only. It is explicitly forbidden from controlling services, launching loops, changing code/artifacts, or running database-writing commands. |
| Windows keepalive | `Dejoia-Kalshi-WSL-Keepalive` only runs `kalshi-scheduler-keepalive sleep infinity` | Retained. It keeps WSL alive and does not control the scheduler. |
| Coverage timer | `kalshi-market-coverage-refresh.timer` runs `current_market_coverage_snapshot.py` | Retained. The script opens SQLite with `mode=ro`, creates TEMP tables only, and atomically writes a report artifact. It is not a production-DB writer. |
| Secondary DB write path | The read-only UI defaulted to `init_db()`, which can run `CREATE TABLE IF NOT EXISTS` and journal pragmas | Fixed: `UI_READ_ONLY=true` now selects `make_sqlite_read_only_engine()`. Live file descriptor is `rr`, not `ur`. |
| Duplicate launch wrappers | Root and `scripts/local/` fixed-rate/keepalive wrappers coexist | `scripts/local/kalshi-fixed-rate-refresh.sh` is deployed. Root wrappers are classified duplicated pending Phase 2 path consolidation; no new launcher was added. |

Safety preflight after reconciliation: `paper_orders=204`,
`position_sizing_decisions=239/239`, `advanced_risk_decisions=239/239`.
Order 204 remains `KXRAINAUSM-26AUG-1`, forecast 523912, status `FILLED`;
the invariant artifact remains `HEALTHY` with no alerts.

## Capability inventory

| Capability | Classification | Existing implementation | Evidence / connection finding |
|---|---|---|---|
| Fixed-rate refresh runtime | VERIFIED_WORKING | `scripts/local/kalshi-fixed-rate-refresh.sh`; user systemd service | Active under systemd; current cycle/stage telemetry is publishing. Some weather/optional stages still fail or time out, which is runtime performance work rather than absence of the runtime. |
| Unified runtime-health tracking | VERIFIED_WORKING | `scripts/fixed_rate_health_status.py`; `research/fixed_rate_health.py`; `reports/fixed_rate_health_status.json` | Current artifact contains cycle, scheduler, source and per-stage timestamps, status, deadlines and timeout reasons. |
| Crypto active-market refresh | VERIFIED_WORKING | Phase 3BC-R3 pipeline and fixed-rate `active_crypto_snapshot_forecast` stage | Current stage completed successfully in the live cycle and published its report. |
| Crypto ranking repair | VERIFIED_WORKING | Phase 3BC-R7 ranking coverage repair | Live scheduler completed and published ranking-repair output. |
| Crypto event quote collector | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `scripts/crypto_event_quote_collector.py` | Tests and artifacts exist; collector is not a critical fixed-rate stage and current complete-vector yield is not yet proven at target cohort size. |
| Crypto event distribution model | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `crypto/distribution_model.py`; `scripts/crypto_distribution_walk_forward.py` | Challenger implementation and tests exist; not registered as a production champion. |
| Crypto multiclass walk-forward | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `scripts/crypto_event_multiclass_walk_forward.py` | Interval-aware scoring exists; statistically valid independent settled cohort remains the gating evidence. |
| Crypto settlement harvesting | VERIFIED_WORKING | `scripts/crypto_exact_settlement_harvest.py`; settlement recovery tests | Historical exact-ticker settlement rows were harvested and used by prior calibration audits. |
| Crypto liquidity coverage | VERIFIED_WORKING | `scripts/crypto_liquidity_coverage_status.py`; `research/liquidity_priority.py` | Current coverage and family rejection telemetry artifacts exist. |
| Crypto liquidity-window diagnosis | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `scripts/crypto_liquidity_window_diagnosis.py`; `research/liquidity_window.py` | Diagnostic logic/tests exist; improvement in complete aligned cohort yield is not yet statistically established. |
| Probability-polytope alignment | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `research/probability_polytope.py`; `scripts/crypto_forecast_polytope_alignment.py` | Bounds, simplex-volume gate and forecast/capture alignment are tested; activation remains correctly gated. |
| Targeted crypto forecast telemetry | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `scripts/crypto_targeted_forecast_telemetry.py` | Rolling telemetry exists, but aligned-yield improvement by family still needs a settled cohort. |
| Historical weather harvesting | VERIFIED_WORKING | CLIAUS historical/monthly scripts and station observation modules | Live cycle completed the CLIAUS historical monthly harvest; 91 monthly samples were previously built. |
| Monthly rainfall calibration | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `weather/monthly_rain.py`; CLIAUS prepare/walk-forward scripts | No-leakage calibration exists and is consumed for Austin monthly rain, but current NOAA preparation is intermittently failing. |
| Supported weather-market preparation | PARTIALLY_IMPLEMENTED | `scripts/supported_weather_prepare.py` | Exact-series/station logic exists; current live stage failed and therefore lacks reliable current-cycle coverage. |
| Weather snapshot/forecast preparation | PARTIALLY_IMPLEMENTED | `scripts/supported_weather_snapshot_forecast.py` | Current live stage timed out at 120 seconds. Do not duplicate it; profile/split the existing implementation. |
| Weather coherent preflight | PARTIALLY_IMPLEMENTED | `scripts/scoped_weather_depth_preflight.py`; `scripts/weather_fast_preflight_soak.py` | Coherent quote and cached evidence path exists; current preflight/cache stages failed. |
| Weather fast-soak logic | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `weather_fast_preflight_soak.py` and tests | Implemented and tested; the 24-cycle healthy runtime evidence target is incomplete. |
| Shadow signal capture | IMPLEMENTED_NOT_CONNECTED | `scripts/shadow_signal_capture.py` | Standalone research capture exists; it is deliberately isolated from guarded paper tables and not a canonical scheduled lane yet. |
| Shadow paper ledger | IMPLEMENTED_NOT_CONNECTED | `scripts/shadow_paper_ledger.py` | Ledger and tests exist; no single canonical Shadow subsystem/service owns its lifecycle. |
| Shadow settlement audit | IMPLEMENTED_NOT_CONNECTED | `scripts/shadow_settlement_audit.py` | Audit exists, but it is not yet connected to one canonical settlement reconciler. |
| One-contract paper activation | VERIFIED_WORKING | `paper/activation.py`; one-contract command/tests | Order 204 proves the operator-approved, one-contract, idempotent path. It remains disabled for further orders. |
| Paper invariant monitor | VERIFIED_WORKING | `paper/invariant_monitor.py`; `scripts/paper_activation_invariant_monitor.py` | Current artifact is `HEALTHY`, order/fill cardinality is one, attribution IDs remain 231/231. |
| Settlement reliability | VERIFIED_WORKING | `paper/settlement_reconciliation.py` plus settlement reliability/remediation modules | Guarded order settlement monitoring is active; final payout remains pending external Kalshi settlement. Multiple older settlement entry points should be consolidated in Phase 21, not recreated. |
| Dynamic position sizing | VERIFIED_WORKING | `position_sizing/` | Phase 3M record 231 is attributable to order 204; production decisions are capped at 239 and shadow writes are isolated. |
| Advanced risk | VERIFIED_WORKING | `advanced_risk/` | Phase 3N record 231 approved the authorized order; production decisions remain capped at 239. Controls remain enabled. |
| Fixed-rate scheduler UI | VERIFIED_WORKING | unified shell status artifact, `/api/ui/status-artifact`, Today/System UI | Status API responds; UI now uses a true read-only SQLite engine and a 15-second shared Today cache. Cold Today render is about 4.8 seconds and cached renders about 0.02 seconds. |
| Crypto cohort-progress UI | IMPLEMENTED_NEEDS_RUNTIME_EVIDENCE | `ui/cohort_progress.py`; `/crypto-cohort-progress`; template/tests | UI exists; the eligible settled cohort is below the statistical activation threshold. |

## Duplication and obsolete-path findings

1. **DUPLICATED — phase-number weather orchestration.** Numerous `phase3bb_r42` through
   `phase3bb_r60` modules wrap overlapping discovery, linking, feature, ranking, and cadence
   behavior. They contain useful evidence but should be reduced to thin compatibility wrappers
   around the existing `weather/` package; do not build another weather pipeline.
2. **DUPLICATED — crypto research definitions.** Event identity, executable quote,
   alignment and usable-sample rules are split between scripts, `crypto/`, and `research/`.
   Phase 9 should consolidate these definitions without replacing the working `crypto_v2` lane.
3. **DUPLICATED — launcher wrappers.** Both root and `scripts/local/` fixed-rate/keepalive
   scripts exist. Only the deployed local fixed-rate launcher is authoritative.
4. **OBSOLETE — paused crypto accelerator automation.** The old
   `kalshi-paper-ready-crypto-accelerator` targets `~/projects/kalshi-predictive-bot`, may stop
   the UI, and can run `accelerate-learning`. It is paused and must remain paused; it conflicts
   with the authoritative runtime and current paper-order safety policy.
5. **DEAD/BUILD ARTIFACTS — tracked or copied `__pycache__` trees.** These are not
   capabilities and must not be used as implementation evidence.

## Connection priorities (no new competing systems)

1. Repair the existing supported-weather prepare/snapshot/preflight chain and NOAA current
   output classification; do not add a second weather scheduler.
2. Move reusable crypto research definitions behind one package API while preserving current
   scripts as thin wrappers.
3. Connect existing shadow capture, ledger and settlement audit behind one explicit Shadow lane,
   isolated from guarded paper tables.
4. Consolidate settlement consumers onto the existing reconciliation definitions.
5. Formalize development/runtime/data/report roots in Phase 2 before moving code or artifacts.

## Phase 1 conclusion

The repository's main gap is connection and runtime evidence, not missing feature count.
Twenty-eight named capabilities were found; no replacement subsystem is warranted. The current
highest operational defect is the supported-weather chain, while the highest architectural
duplication is phase-number orchestration around otherwise viable crypto and weather packages.
