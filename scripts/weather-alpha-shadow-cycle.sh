#!/usr/bin/env bash
set -euo pipefail

: "${KALSHI_APP_PATH:?Set KALSHI_APP_PATH to the authoritative checkout}"
: "${KALSHI_WRITER_LOCK:?Set KALSHI_WRITER_LOCK to the existing shared writer lock}"

export UI_READ_ONLY=true
export EXECUTION_ENABLED=false
export EXECUTION_DRY_RUN=true
export EXECUTION_KILL_SWITCH=true
export AUTOPILOT_ENABLED=false
export AUTOPILOT_DRY_RUN=true
export PAPER_ORDER_CREATION_ENABLED=false

cd "$KALSHI_APP_PATH"
exec 9>"$KALSHI_WRITER_LOCK"
if ! flock -n 9; then
  echo "Weather alpha shadow cycle deferred: shared writer lock is busy."
  exit 0
fi

writer_status=$(.venv/bin/kalshi-bot db-writer-monitor --json)
if ! grep -q '"safe_to_start_write": true' <<<"$writer_status"; then
  echo "Weather alpha shadow cycle deferred: writer monitor is not clear."
  exit 0
fi

for location in new_york chicago miami austin los_angeles boston washington_dc; do
  .venv/bin/kalshi-bot ingest-weather --location-key "$location"
  .venv/bin/kalshi-bot build-weather-features --location-key "$location" --limit 200
done
.venv/bin/kalshi-bot link-weather-markets
.venv/bin/kalshi-bot forecast --model weather_v2 --limit 500
.venv/bin/kalshi-bot sync-settlements --lookback-days 30 --limit 100 --max-pages 0
.venv/bin/kalshi-bot weather-alpha-validation --output-dir reports/weather_alpha_validation
