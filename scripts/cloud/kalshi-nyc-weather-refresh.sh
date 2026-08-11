#!/usr/bin/env bash
set -euo pipefail

APP_PATH=/opt/kalshi-predictive-bot
WRITER_LOCK=/var/lib/kalshi-bot/kalshi-writer.lock

cd "$APP_PATH"
exec 9>"$WRITER_LOCK"
if ! flock -n 9; then
  echo "Weather refresh deferred because the shared writer lock is busy."
  exit 0
fi

writer_status=$(.venv/bin/kalshi-bot db-writer-monitor --json)
if ! grep -q '"safe_to_start_write": true' <<<"$writer_status"; then
  echo "Weather refresh deferred because db-writer-monitor is not clear."
  exit 0
fi

for location in new_york chicago miami austin los_angeles boston washington_dc; do
  .venv/bin/kalshi-bot ingest-weather --location-key "$location"
  .venv/bin/kalshi-bot build-weather-features --location-key "$location" --limit 200
done
