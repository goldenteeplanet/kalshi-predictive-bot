#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
DB=/var/lib/kalshi-bot/kalshi_phase1.db
[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
holders=$(.venv/bin/kalshi-bot db-writer-monitor --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["holder_count"])')
[[ "$holders" -eq 0 ]]
before_f=$(sqlite3 "$DB" "SELECT COUNT(*) FROM forecasts WHERE model_name='weather_v2';")
before_e=$(sqlite3 "$DB" "SELECT COUNT(*) FROM runtime_provenance_events WHERE stage='FORECAST_CREATED' AND model_name='weather_v2';")
start=$(date +%s%N)
output=$(.venv/bin/kalshi-bot forecast --model weather_v2 --limit 20 2>&1)
end=$(date +%s%N)
after_f=$(sqlite3 "$DB" "SELECT COUNT(*) FROM forecasts WHERE model_name='weather_v2';")
after_e=$(sqlite3 "$DB" "SELECT COUNT(*) FROM runtime_provenance_events WHERE stage='FORECAST_CREATED' AND model_name='weather_v2';")
printf '%s\n' "$output"
printf 'model=weather_v2 elapsed_ms=%s forecasts_added=%s events_added=%s\n' \
  "$(((end-start)/1000000))" "$((after_f-before_f))" "$((after_e-before_e))"
