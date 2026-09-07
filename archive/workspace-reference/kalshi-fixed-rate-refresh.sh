#!/usr/bin/env bash
set -euo pipefail
trap 'status=$?; printf "Refresh loop error: status=%s line=%s command=%s\n" "$status" "$LINENO" "$BASH_COMMAND" >&2; exit "$status"' ERR

cd "${KALSHI_RUNTIME_ROOT:-/home/james/kalshi-runtime-src}"
set -a
source .env
set +a

export UI_READ_ONLY=true
export EXECUTION_ENABLED=false
export EXECUTION_DRY_RUN=true
export EXECUTION_KILL_SWITCH=true
export AUTOPILOT_ENABLED=false
export AUTOPILOT_DRY_RUN=true
export PAPER_ORDER_CREATION_ENABLED=false
export PAPER_ORDER_KILL_SWITCH=true

readonly INTERVAL_SECONDS="${KALSHI_REFRESH_INTERVAL_SECONDS:-900}"
readonly MIN_POST_CYCLE_COOLDOWN_SECONDS="${KALSHI_MIN_POST_CYCLE_COOLDOWN_SECONDS:-60}"
readonly WRITER_LOCK="${KALSHI_WRITER_LOCK:-/home/james/kalshi-local-runtime/kalshi-writer.lock}"
readonly LOOP_LOCK="${KALSHI_REFRESH_LOOP_LOCK:-/home/james/kalshi-local-runtime/kalshi-refresh-loop.lock}"
readonly DATABASE_PATH="${KALSHI_DATABASE_PATH:-/home/james/kalshi-predictive-bot-data/kalshi_phase1.db}"
readonly UI_REPORT_ROOT="${KALSHI_UI_REPORT_ROOT:-/mnt/c/Users/user1/OneDrive/Documents/Dejoia Trading Bot/kalshi-predictive-bot/reports}"
readonly UI_REPORT_PUBLISH_ENABLED="${KALSHI_UI_REPORT_PUBLISH_ENABLED:-false}"
readonly HEALTH_STATUS="reports/fixed_rate_health_status.json"

update_health_status() {
  .venv/bin/python scripts/fixed_rate_health_status.py "$@" \
    --output "$HEALTH_STATUS" >/dev/null 2>&1 || true
}

run_health_stage() {
  local stage_name="$1"
  local timeout_seconds="$2"
  shift 2
  local stage_started_at command_status
  stage_started_at="$(date -Is)"
  set +e
  "$@"
  command_status=$?
  set -e
  update_health_status stage --stage "$stage_name" \
    --stage-started-at "$stage_started_at" --exit-code "$command_status" \
    --timeout-seconds "$timeout_seconds" \
    --coinbase-report reports/phase_gh2/crypto_staging/stage_status.json \
    --gh2-report reports/phase_gh2/gh2_active_candidate_refresh.json
}

mkdir -p "$(dirname "$LOOP_LOCK")"
exec 9>"$LOOP_LOCK"
if ! flock -n 9; then
  printf 'Refresh loop already active; refusing duplicate launcher.\n' >&2
  exit 0
fi

publish_report_dir() {
  local relative_dir="$1"
  local source_dir="reports/${relative_dir}"
  local target_dir="${UI_REPORT_ROOT}/${relative_dir}"
  local source_file target_file temp_file

  [[ "$UI_REPORT_PUBLISH_ENABLED" == "true" ]] || return 0
  [[ -d "$source_dir" ]] || return 0
  if ! mkdir -p "$target_dir"; then
    printf 'Warning: UI report publish unavailable for %s\n' "$target_dir" >&2
    return 0
  fi
  while IFS= read -r -d '' source_file; do
    target_file="${target_dir}/$(basename "$source_file")"
    temp_file="${target_file}.tmp.$$"
    if ! cp -- "$source_file" "$temp_file" || ! mv -f -- "$temp_file" "$target_file"; then
      printf 'Warning: UI report publish failed for %s\n' "$target_file" >&2
      rm -f -- "$temp_file" 2>/dev/null || true
    fi
  done < <(find "$source_dir" -maxdepth 1 -type f \( -name '*.json' -o -name '*.md' \) -print0)
}

mkdir -p \
  reports/crypto_event_vectors \
  reports/phase_gh2/crypto_staging \
  reports/phase3bc_r3 \
  reports/phase3bc \
  /home/james/kalshi-local-runtime/gh1-staging \
  /home/james/kalshi-local-runtime/watch

next_start_epoch="$(date +%s)"
while true; do
  now_epoch="$(date +%s)"
  if (( now_epoch < next_start_epoch )); then
    sleep "$((next_start_epoch - now_epoch))"
  fi
  cycle_started_epoch="$(date +%s)"
  cycle_started_at="$(date -Is)"
  update_health_status start --cycle-id "$cycle_started_epoch" \
    --cycle-started-at "$cycle_started_at" --cadence-seconds "$INTERVAL_SECONDS"
  date -Is

  # Stage A owns collection, exact snapshot repair, feature construction, and
  # crypto_v2 forecasts. Profiling shows snapshot repair is the dominant cost;
  # keep it independently bounded so ranking/report work cannot consume its
  # deadline or hide its timeout attribution.
  run_health_stage active_crypto_snapshot_forecast 480 timeout 480s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/kalshi-bot phase3bc-r3-active-crypto-refresh \
      --output-dir reports/phase3bc_r3 \
      --phase3bc-output-dir reports/phase3bc \
      --refresh-open-markets --market-limit 100 --market-max-pages 2 \
      --crypto-series-tickers KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE \
      --crypto-market-scan-limit 10000 --crypto-link-limit 1000 \
      --forecast-limit 1000 --opportunity-limit 150 --phase3bc-limit 1000 \
      --forecast-current-windows-only --near-money-only \
      --near-money-per-symbol-limit 8 --near-money-window-limit 4 \
      --snapshot-fetch-concurrency 4 --skip-opportunity-report \
      --defer-phase3bc-router --cycles 1

  # The router is report-only but can take several minutes over retained links.
  # Keep it outside the snapshot/forecast writer transaction so its cost has an
  # independent deadline and cannot obscure current-market freshness.
  run_health_stage active_crypto_router 210 timeout 210s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/kalshi-bot phase3bc-crypto-clean-opportunity-router \
      --output-dir reports/phase3bc --limit 1000

  # Stage B performs the bounded current-window ranking write after the fresh
  # snapshot/forecast transaction has committed.
  run_health_stage active_crypto_ranking_finalize 240 timeout 240s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/kalshi-bot phase3bc-r7-crypto-ranking-coverage-repair \
      --output-dir reports/phase3bc_r7 --limit 1000 \
      --freshness-minutes 15 --repair-rankings --repair-limit 150

  # Publish watcher truth immediately after the guarded market refresh so the
  # Today page does not wait behind research collection and settlement work.
  .venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5 || true

  # Publish the global UI freshness strip as soon as the critical market stage
  # is complete. A second refresh after settlement keeps end-of-cycle truth.
  run_health_stage ui_shell_status_refresh_early 60 timeout 60s \
    .venv/bin/kalshi-bot ui-shell-status-refresh \
      --output-path reports/ui/shell_status_snapshot.json

  run_health_stage weather_catalog_refresh 180 timeout 180s flock -w 45 "$WRITER_LOCK" \
    bash -lc '
      set -euo pipefail
      for series in KXTEMPNYCH KXRAINAUSM KXRAINSTPM; do
        .venv/bin/kalshi-bot sync-markets --status open --limit 100 \
          --max-pages 2 --series-ticker "$series"
      done
    '

  run_health_stage supported_weather_prepare 150 timeout 150s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/python scripts/supported_weather_prepare.py \
      --series KXTEMPNYCH,KXRAINAUSM,KXRAINSTPM \
      --output reports/phase_gh2/supported_weather_prepare.json \
      --per-command-timeout 45

  run_health_stage cliaus_historical_monthly_harvest 90 timeout 90s \
    .venv/bin/python scripts/cliaus_historical_monthly_harvest.py \
      --start 2018-01-01 --cutoff-day 20 --minimum-training-months 12 \
      --output reports/phase_gh2/cliaus_historical_monthly_harvest.json

  run_health_stage cliaus_monthly_rain_prepare 45 timeout 45s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/python scripts/cliaus_monthly_rain_prepare.py \
      --output reports/phase_gh2/cliaus_monthly_rain_prepare.json \
      --historical-calibration reports/phase_gh2/cliaus_historical_monthly_harvest.json \
      --minimum-calibration-samples 12

  run_health_stage supported_weather_snapshot_forecast 240 timeout 240s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/python scripts/supported_weather_snapshot_forecast.py \
      --preparation reports/phase_gh2/supported_weather_prepare.json \
      --output reports/phase_gh2/supported_weather_snapshot_forecast.json \
      --limit 8 --fetch-workers 4

  run_health_stage coinbase_stage 45 timeout 45s \
    .venv/bin/kalshi-bot gh2-stage-crypto-quotes \
    --staging-dir reports/phase_gh2/crypto_staging \
    --symbols BTC,ETH,SOL,XRP,DOGE --sources coinbase --max-workers 4

  # Publish current Coinbase/NOAA decision truth before the API-heavy weather
  # paper-gate diagnostic. The latter is read-only and has its own deadline.
  run_health_stage gh2_decision_refresh 300 timeout 300s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/kalshi-bot gh2-single-writer-decision-refresh --apply \
      --output-dir reports/phase_gh2 --reports-dir reports \
      --crypto-staging-dir reports/phase_gh2/crypto_staging \
      --gh1-staging-dir /home/james/kalshi-local-runtime/gh1-staging \
      --candidate-manifest-path /home/james/kalshi-local-runtime/watch/actionable_tickers.json \
      --candidate-limit 40 --active-link-limit 24 --forecast-limit 24 \
      --opportunity-limit 20 --freshness-minutes 15 --soak-cycles-required 24 \
      --defer-weather-gate --guard-active-writer

  # Keep the current-weather diagnostic inside a short API budget. The coherent
  # preflight below owns exact BUY-side depth for the two guarded candidates.
  run_health_stage weather_gate_diagnostics 75 timeout 75s \
    .venv/bin/kalshi-bot phase3ba-r3-weather-paper-gate \
      --output-dir reports/phase3ba_r3 --reports-dir reports \
      --limit 12 --current-window-lookback-hours 3 --match-tolerance-hours 3 \
      --deadline-seconds 50 --batch-size 2

  # Phase 3M's historical scan is deliberately separated from the fresh-quote
  # transaction. A valid six-hour cache is reused; refreshes remain no-lookahead.
  run_health_stage weather_phase3m_cache_prepare 300 timeout 300s \
    .venv/bin/python scripts/scoped_weather_depth_preflight.py \
      --prepare-only \
      --gate reports/phase3ba_r3/weather_paper_gate.json \
      --cache reports/phase3ba_r3/phase3m_historical_evidence_cache.json \
      --output reports/phase3ba_r3/scoped_weather_depth_preflight.json \
      --ticker KXRAINAUSM-26AUG-1 --ticker KXRAINAUSM-26AUG-2 || true

  # Capture exact books, forecast, rank, size, and risk-check in one bounded
  # writer stage. Execution and paper-order creation remain disabled above.
  run_health_stage weather_fast_coherent_preflight 90 timeout 90s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/python scripts/scoped_weather_depth_preflight.py \
      --gate reports/phase3ba_r3/weather_paper_gate.json \
      --cache reports/phase3ba_r3/phase3m_historical_evidence_cache.json \
      --state reports/phase3ba_r3/scoped_weather_preflight_pair_state.json \
      --output reports/phase3ba_r3/scoped_weather_depth_preflight.json \
      --ticker KXRAINAUSM-26AUG-1 --ticker KXRAINAUSM-26AUG-2 || true

  run_health_stage weather_gate_post_preflight 75 timeout 75s \
    .venv/bin/kalshi-bot phase3ba-r3-weather-paper-gate \
      --output-dir reports/phase3ba_r3 --reports-dir reports \
      --limit 8 --current-window-lookback-hours 3 --match-tolerance-hours 3 \
      --deadline-seconds 50 --batch-size 2

  .venv/bin/python scripts/weather_fast_preflight_soak.py \
    --cycle-id "$cycle_started_epoch" \
    --preflight reports/phase3ba_r3/scoped_weather_depth_preflight.json \
    --gate reports/phase3ba_r3/weather_paper_gate.json \
    --history reports/phase3ba_r3/weather_fast_preflight_soak_history.jsonl \
    --output reports/phase3ba_r3/weather_fast_preflight_soak.json \
    --required-cycles 3
  publish_report_dir phase3ba_r3

  .venv/bin/python scripts/crypto_liquidity_window_diagnosis.py \
    --database "$DATABASE_PATH" \
    --output reports/crypto_event_vectors/liquidity_window_diagnosis.json || true

  # Shard capture by family and reduce concurrency to avoid Kalshi HTTP 429
  # bursts. Each shard has an independent deadline and publishes partial
  # progress, so one slow family cannot consume the whole collection budget.
  # Alternate one small targeted-capture shard per cycle. Each shard is capped
  # at two candidate events, so collector research cannot consume two full
  # stage budgets or overlap the next intended cadence.
  if (( cycle_started_epoch % 2 == 0 )); then
    targeted_capture_stage="targeted_crypto_capture_major"
    targeted_capture_series="KXBTC,KXETH"
  else
    targeted_capture_stage="targeted_crypto_capture_alt"
    targeted_capture_series="KXSOLE,KXXRP,KXDOGE"
  fi
  run_health_stage "$targeted_capture_stage" 180 timeout 180s flock -w 45 "$WRITER_LOCK" \
    .venv/bin/python scripts/crypto_event_quote_collector.py \
      --output reports/crypto_event_vectors/status.json \
      --backfill-report reports/phase3bc_r3/phase3bc_r3_active_crypto_refresh.json \
      --series "$targeted_capture_series" \
      --coherence-ms 2500 --max-workers 2 \
      --max-new-events 1 --max-events-attempted 2 \
      --liquidity-window-report reports/crypto_event_vectors/liquidity_window_diagnosis.json \
      --max-forecast-lag-minutes 30 \
      --targeted-forecast-events 1 \
      --targeted-capture-latency-seconds 30 \
      --targeted-capture-max-buckets 25 \
      --canary-required 5 --target 100
  .venv/bin/python scripts/crypto_forecast_polytope_alignment.py \
    --database "$DATABASE_PATH" \
    --output reports/crypto_event_vectors/forecast_polytope_alignment.json \
    --model-name crypto_v2 --max-lag-minutes 30 --max-coherence-ms 2500 || true
  .venv/bin/python scripts/crypto_targeted_forecast_telemetry.py \
    --collector reports/crypto_event_vectors/status.json \
    --alignment reports/crypto_event_vectors/forecast_polytope_alignment.json \
    --output reports/crypto_event_vectors/targeted_forecast_telemetry.json || true
  .venv/bin/python scripts/crypto_liquidity_coverage_status.py \
    --database "$DATABASE_PATH" \
    --alignment-manifest reports/crypto_event_vectors/forecast_polytope_alignment.json \
    --output reports/crypto_event_vectors/liquidity_coverage_status.json || true

  .venv/bin/python scripts/shadow_signal_capture.py \
    --r5 reports/phase3bc_r5/phase3bc_r5_crypto_freshness_watch.json \
    --output reports/overnight_alpha_factory/shadow_signals.jsonl || true
  .venv/bin/python scripts/shadow_paper_ledger.py \
    --signals reports/overnight_alpha_factory/shadow_signals.jsonl \
    --trades reports/overnight_alpha_factory/shadow_trades.jsonl \
    --report reports/overnight_alpha_factory/shadow_trade_status.json || true

  run_health_stage settlement_refresh 600 timeout 600s flock -w 45 "$WRITER_LOCK" bash -lc '
    set -euo pipefail
    .venv/bin/kalshi-bot sync-settlements \
      --lookback-days 90 --limit 200 --max-pages 10
    .venv/bin/kalshi-bot paper-pnl --skip-signal-refresh
    .venv/bin/kalshi-bot phase3aa-realize \
      --no-dry-run --no-sync-settlements --limit 1000 \
      --output-dir reports/phase3aa
    .venv/bin/kalshi-bot paper-settlement-doctor \
      --limit 1000 --output-dir reports/paper_settlement_reconciliation
  '
  run_health_stage paper_activation_invariants 30 timeout 30s \
    .venv/bin/python scripts/paper_activation_invariant_monitor.py \
      --output reports/paper_activation/invariant_status.json
  publish_report_dir paper_activation
  # Recompute after settlement sync. The gate runner records state only after a
  # successful frozen-policy evaluation, so timeouts and failures are retryable.
  .venv/bin/python scripts/crypto_liquidity_coverage_status.py \
    --database "$DATABASE_PATH" \
    --alignment-manifest reports/crypto_event_vectors/forecast_polytope_alignment.json \
    --output reports/crypto_event_vectors/liquidity_coverage_status.json || true
  timeout 930s .venv/bin/python scripts/crypto_cohort_gate_runner.py \
    --status reports/crypto_event_vectors/liquidity_coverage_status.json \
    --state reports/crypto_event_vectors/cohort_gate_state.json \
    --database "$DATABASE_PATH" \
    --output reports/crypto_event_vectors/multiclass_interval_scoring.json \
    --alignment-manifest reports/crypto_event_vectors/forecast_polytope_alignment.json \
    --timeout-seconds 900 || true
  publish_report_dir phase_gh2
  publish_report_dir crypto_event_vectors
  publish_report_dir phase3bc_r5
  publish_report_dir overnight_alpha_factory
  publish_report_dir phase3aa
  publish_report_dir paper_settlement_reconciliation
  update_health_status finish --cadence-seconds "$INTERVAL_SECONDS"

  next_start_epoch="$((cycle_started_epoch + INTERVAL_SECONDS))"
  now_epoch="$(date +%s)"
  cooldown_start_epoch="$((now_epoch + MIN_POST_CYCLE_COOLDOWN_SECONDS))"
  if (( next_start_epoch < cooldown_start_epoch )); then
    next_start_epoch="$cooldown_start_epoch"
  fi
done
