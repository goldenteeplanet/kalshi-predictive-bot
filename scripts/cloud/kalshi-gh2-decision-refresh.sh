#!/usr/bin/env bash
set -euo pipefail

APP_PATH=/opt/kalshi-predictive-bot
WRITER_LOCK=/var/lib/kalshi-bot/kalshi-writer.lock
GH2_ROOT=/var/lib/kalshi-bot-gh2
GH1_ROOT=/var/lib/kalshi-bot-gh1

cd "$APP_PATH"
mkdir -p "$GH2_ROOT/crypto-staging" "$GH2_ROOT/reports" "$GH1_ROOT/watch"

# Network fetches stage to files before the shared SQLite writer lock is acquired.
.venv/bin/kalshi-bot gh2-stage-crypto-quotes \
  --staging-dir "$GH2_ROOT/crypto-staging" \
  --symbols BTC,ETH,SOL,XRP,DOGE \
  --sources coinbase \
  --max-workers 4

exec 9>"$WRITER_LOCK"
if ! flock -n 9; then
  echo "GH-2 refresh deferred because the shared writer lock is busy."
  exit 0
fi

writer_status=$(.venv/bin/kalshi-bot db-writer-monitor --json)
if ! grep -q '"safe_to_start_write": true' <<<"$writer_status"; then
  echo "GH-2 refresh deferred because db-writer-monitor is not clear."
  exit 0
fi

exec .venv/bin/kalshi-bot gh2-single-writer-decision-refresh \
  --apply \
  --output-dir "$GH2_ROOT/reports" \
  --reports-dir "$APP_PATH/reports" \
  --crypto-staging-dir "$GH2_ROOT/crypto-staging" \
  --gh1-staging-dir "$GH1_ROOT/staging" \
  --candidate-manifest-path "$GH1_ROOT/watch/actionable_tickers.json" \
  --candidate-limit 40 \
  --active-link-limit 250 \
  --forecast-limit 250 \
  --opportunity-limit 100 \
  --freshness-minutes 15 \
  --soak-cycles-required 24 \
  --guard-active-writer
