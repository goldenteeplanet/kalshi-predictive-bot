#!/usr/bin/env bash
set -euo pipefail

APP_PATH=/opt/kalshi-predictive-bot
WRITER_LOCK=/var/lib/kalshi-bot/kalshi-writer.lock
GH2_ROOT=/var/lib/kalshi-bot-gh2
GH1_ROOT=/var/lib/kalshi-bot-gh1
STATUS_PATH="$GH2_ROOT/reports/gh2_scheduler_status.json"
SERVICE_STARTED_EPOCH=$(date +%s)
LOCK_WAIT_SECONDS=0
WRITER_RUNTIME_SECONDS=0

cd "$APP_PATH"
mkdir -p "$GH2_ROOT/crypto-staging" "$GH2_ROOT/reports" "$GH1_ROOT/watch"

write_scheduler_status() {
  local status=$1
  local deferred_reason=${2:-}
  GH2_STATUS_PATH="$STATUS_PATH" \
  GH2_STATUS="$status" \
  GH2_DEFERRED_REASON="$deferred_reason" \
  GH2_LOCK_WAIT_SECONDS="$LOCK_WAIT_SECONDS" \
  GH2_WRITER_RUNTIME_SECONDS="$WRITER_RUNTIME_SECONDS" \
  GH2_SERVICE_STARTED_EPOCH="$SERVICE_STARTED_EPOCH" \
    .venv/bin/python - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

path = Path(os.environ["GH2_STATUS_PATH"])
try:
    previous = json.loads(path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError, OSError):
    previous = {}
now = datetime.now(UTC)
status = os.environ["GH2_STATUS"]
payload = {
    "generated_at": now.isoformat(),
    "status": status,
    "deferred_cycle_reason": os.environ.get("GH2_DEFERRED_REASON") or None,
    "lock_wait_seconds": float(os.environ.get("GH2_LOCK_WAIT_SECONDS") or 0),
    "writer_runtime_seconds": float(os.environ.get("GH2_WRITER_RUNTIME_SECONDS") or 0),
    "service_runtime_seconds": max(
        0,
        int(now.timestamp()) - int(os.environ["GH2_SERVICE_STARTED_EPOCH"]),
    ),
    "last_successful_completion": (
        now.isoformat()
        if status == "COMPLETE"
        else previous.get("last_successful_completion")
    ),
    "paper_order_creation_enabled": False,
    "live_execution_enabled": False,
    "autopilot_enabled": False,
}
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
temporary.replace(path)
PY
}

write_scheduler_status "STAGING"

# Network fetches stage to files before the shared SQLite writer lock is acquired.
if ! .venv/bin/kalshi-bot gh2-stage-crypto-quotes \
  --staging-dir "$GH2_ROOT/crypto-staging" \
  --symbols BTC,ETH,SOL,XRP,DOGE \
  --sources coinbase \
  --max-workers 4; then
  write_scheduler_status "FAILED" "QUOTE_STAGE_FAILED"
  exit 1
fi

exec 9>"$WRITER_LOCK"
write_scheduler_status "WAITING_FOR_WRITER"
lock_wait_started=$(date +%s)
if ! flock -w 45 9; then
  LOCK_WAIT_SECONDS=$(( $(date +%s) - lock_wait_started ))
  write_scheduler_status "DEFERRED" "SHARED_WRITER_LOCK_BUSY"
  echo "GH-2 refresh deferred after waiting 45 seconds for the shared writer lock."
  exit 0
fi
LOCK_WAIT_SECONDS=$(( $(date +%s) - lock_wait_started ))
export GH2_LOCK_WAIT_SECONDS="$LOCK_WAIT_SECONDS"

writer_status=$(.venv/bin/kalshi-bot db-writer-monitor --json)
if ! grep -q '"safe_to_start_write": true' <<<"$writer_status"; then
  write_scheduler_status "DEFERRED" "DB_WRITER_MONITOR_BUSY"
  echo "GH-2 refresh deferred because db-writer-monitor is not clear."
  exit 0
fi

write_scheduler_status "RUNNING"
writer_started=$(date +%s)
if .venv/bin/kalshi-bot gh2-single-writer-decision-refresh \
  --apply \
  --output-dir "$GH2_ROOT/reports" \
  --reports-dir "$APP_PATH/reports" \
  --crypto-staging-dir "$GH2_ROOT/crypto-staging" \
  --gh1-staging-dir "$GH1_ROOT/staging" \
  --candidate-manifest-path "$GH1_ROOT/watch/actionable_tickers.json" \
  --active-market-catalog-path "$GH1_ROOT/watch/active_market_catalog.json" \
  --candidate-limit 40 \
  --active-link-limit 24 \
  --forecast-limit 24 \
  --opportunity-limit 20 \
  --freshness-minutes 15 \
  --soak-cycles-required 24 \
  --guard-active-writer; then
  WRITER_RUNTIME_SECONDS=$(( $(date +%s) - writer_started ))
  write_scheduler_status "COMPLETE"
else
  command_status=$?
  WRITER_RUNTIME_SECONDS=$(( $(date +%s) - writer_started ))
  write_scheduler_status "FAILED" "GH2_DECISION_REFRESH_FAILED"
  exit "$command_status"
fi
