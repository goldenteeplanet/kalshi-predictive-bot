#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
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
export KALSHI_WEBSOCKET_ENABLED=true
export KALSHI_WEBSOCKET_STAGING_DIR=/home/james/kalshi-local-runtime/gh1-staging

mkdir -p \
  /home/james/kalshi-local-runtime/gh1-staging \
  /home/james/kalshi-local-runtime/watch

exec .venv/bin/kalshi-bot gh1-websocket-orderbook-watch \
  --connect \
  --stream-max-seconds 60 \
  --discovery-refresh-seconds 300 \
  --healthy-cycle-delay-seconds 2 \
  --reconnect-initial-seconds 5 \
  --reconnect-max-seconds 120 \
  --status-path /home/james/kalshi-local-runtime/watch/gh1_status.json \
  --preferred-tickers-path /home/james/kalshi-local-runtime/watch/actionable_tickers.json \
  --max-preferred-tickers 40
