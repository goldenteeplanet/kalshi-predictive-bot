#!/usr/bin/env bash
set -euo pipefail

APP_PATH=/opt/kalshi-predictive-bot
ENV_PATH=/etc/kalshi-bot/kalshi-bot.env

cd "$APP_PATH"
existing_pids=$(pgrep -f 'phase3bc-r5-crypto-freshness-watch' || true)
if [[ -n "$existing_pids" ]]; then
  echo "Refusing duplicate R5 start; existing pid(s): $existing_pids" >&2
  exit 75
fi

set -a
. "$ENV_PATH"
set +a

.venv/bin/kalshi-bot db-writer-monitor --json > /tmp/kalshi-r5-writer-guard.json
if grep -q '"safe_to_start_write": false' /tmp/kalshi-r5-writer-guard.json; then
  echo "Refusing R5 start because db-writer-monitor is not clear." >&2
  exit 75
fi

echo "R5 start guard passed."
