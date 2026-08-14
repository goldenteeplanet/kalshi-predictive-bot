#!/usr/bin/env bash
set -euo pipefail

: "${KALSHI_APP_PATH:?Set KALSHI_APP_PATH to the authoritative checkout}"
: "${KALSHI_WRITER_LOCK:?Set KALSHI_WRITER_LOCK to the existing shared writer lock}"
KALSHI_BOT_BIN=${KALSHI_BOT_BIN:-.venv/bin/kalshi-bot}
KALSHI_SETTLEMENT_MAX_PAGES=${KALSHI_SETTLEMENT_MAX_PAGES:-5}
KALSHI_WEATHER_MARKET_MAX_PAGES=${KALSHI_WEATHER_MARKET_MAX_PAGES:-5}
KALSHI_WEATHER_SERIES=${KALSHI_WEATHER_SERIES:-KXTEMPNYCH}
KALSHI_VALIDATION_CMD=${KALSHI_VALIDATION_CMD:-"$KALSHI_BOT_BIN weather-alpha-validation --output-dir reports/weather_alpha_validation"}
KALSHI_WEATHER_LOCATIONS=${KALSHI_WEATHER_LOCATIONS:-"new_york chicago miami austin los_angeles boston washington_dc"}

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

writer_status=$($KALSHI_BOT_BIN db-writer-monitor --json)
if ! grep -q '"safe_to_start_write": true' <<<"$writer_status"; then
  echo "Weather alpha shadow cycle deferred: writer monitor is not clear."
  exit 0
fi

weather_coordinates() {
  case "$1" in
    new_york) echo "40.7128 -74.0060" ;;
    chicago) echo "41.9742 -87.9073" ;;
    miami) echo "25.7959 -80.2870" ;;
    austin) echo "30.1975 -97.6664" ;;
    los_angeles) echo "33.9416 -118.4085" ;;
    boston) echo "42.3656 -71.0096" ;;
    washington_dc) echo "38.8512 -77.0402" ;;
    *) echo "Unsupported weather location: $1" >&2; return 1 ;;
  esac
}

if [[ "${KALSHI_SKIP_WEATHER_SOURCE_REFRESH:-false}" != "true" ]]; then
  for location in $KALSHI_WEATHER_LOCATIONS; do
    read -r latitude longitude <<<"$(weather_coordinates "$location")"
    $KALSHI_BOT_BIN ingest-weather --location-key "$location" --lat "$latitude" --lon "$longitude"
    $KALSHI_BOT_BIN build-weather-features --location-key "$location" --limit 200
  done
fi
$KALSHI_BOT_BIN collect-once --status open --limit 100 \
  --max-pages "$KALSHI_WEATHER_MARKET_MAX_PAGES" \
  --series-ticker "$KALSHI_WEATHER_SERIES" --include-orderbook
$KALSHI_BOT_BIN link-weather-markets
$KALSHI_BOT_BIN forecast --model weather_v2 --limit 500
$KALSHI_BOT_BIN sync-settlements --lookback-days 60 --limit 100 --max-pages "$KALSHI_SETTLEMENT_MAX_PAGES"

# Validation may use a newer diagnostic worktree while collection uses the deployed runtime.
eval "$KALSHI_VALIDATION_CMD"
