#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
DB=/var/lib/kalshi-bot/kalshi_phase1.db
mkdir -p reports/phase_prov8

run_cycle() {
  model=$1
  before_f=$(sqlite3 "$DB" "SELECT COUNT(*) FROM forecasts WHERE model_name='$model';")
  before_e=$(sqlite3 "$DB" "SELECT COUNT(*) FROM runtime_provenance_events WHERE stage='FORECAST_CREATED' AND model_name='$model';")
  start=$(date +%s%N)
  output=$(.venv/bin/kalshi-bot forecast --model "$model" --limit 20 2>&1)
  end=$(date +%s%N)
  elapsed_ms=$(( (end-start)/1000000 ))
  after_f=$(sqlite3 "$DB" "SELECT COUNT(*) FROM forecasts WHERE model_name='$model';")
  after_e=$(sqlite3 "$DB" "SELECT COUNT(*) FROM runtime_provenance_events WHERE stage='FORECAST_CREATED' AND model_name='$model';")
  printf '%s\n' "$output"
  printf 'model=%s elapsed_ms=%s forecasts_added=%s events_added=%s\n' \
    "$model" "$elapsed_ms" "$((after_f-before_f))" "$((after_e-before_e))"
}

run_cycle crypto_v2 | tee reports/phase_prov8/crypto_cycle.txt
run_cycle weather_v2 | tee reports/phase_prov8/weather_cycle.txt
.venv/bin/kalshi-bot find-opportunities --help
