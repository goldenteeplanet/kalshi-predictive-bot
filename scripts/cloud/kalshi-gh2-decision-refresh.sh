#!/usr/bin/env bash
set -euo pipefail

APP_PATH=/opt/kalshi-predictive-bot
WRITER_LOCK=/var/lib/kalshi-bot/kalshi-writer.lock
GH2_ROOT=/var/lib/kalshi-bot-gh2
GH1_ROOT=/var/lib/kalshi-bot-gh1
STATUS_PATH="$GH2_ROOT/reports/gh2_scheduler_status.json"
STAGE_PATH="$GH2_ROOT/reports/gh2_stage.json"
SERVICE_STARTED_EPOCH=$(date +%s)
INTERNAL_DEADLINE_SECONDS=${GH2_INTERNAL_DEADLINE_SECONDS:-345}
WRITER_BUDGET_SECONDS=${GH2_WRITER_BUDGET_SECONDS:-270}
DIAGNOSTICS_BUDGET_SECONDS=${GH2_DIAGNOSTICS_BUDGET_SECONDS:-45}
LOCK_WAIT_SECONDS=0
WRITER_RUNTIME_SECONDS=0
DIAGNOSTICS_RUNTIME_SECONDS=0
DIAGNOSTICS_STATUS=NOT_STARTED
CORE_COMPLETE=0
FINAL_STATUS_WRITTEN=0
WRITER_STARTED_EPOCH=0

cd "$APP_PATH"
mkdir -p "$GH2_ROOT/crypto-staging" "$GH2_ROOT/reports" "$GH1_ROOT/watch"

if [[ -r /etc/kalshi-bot/kalshi-bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/kalshi-bot/kalshi-bot.env
  set +a
fi

remaining_service_seconds() {
  local elapsed=$(( $(date +%s) - SERVICE_STARTED_EPOCH ))
  local remaining=$(( INTERNAL_DEADLINE_SECONDS - elapsed ))
  if (( remaining < 0 )); then
    remaining=0
  fi
  printf '%s\n' "$remaining"
}

write_scheduler_status() {
  local status=$1
  local deferred_reason=${2:-}
  GH2_STATUS_PATH="$STATUS_PATH" \
  GH2_STAGE_PATH="$STAGE_PATH" \
  GH2_STATUS="$status" \
  GH2_DEFERRED_REASON="$deferred_reason" \
  GH2_LOCK_WAIT_SECONDS="$LOCK_WAIT_SECONDS" \
  GH2_WRITER_RUNTIME_SECONDS="$WRITER_RUNTIME_SECONDS" \
  GH2_DIAGNOSTICS_RUNTIME_SECONDS="$DIAGNOSTICS_RUNTIME_SECONDS" \
  GH2_DIAGNOSTICS_STATUS="$DIAGNOSTICS_STATUS" \
  GH2_SERVICE_STARTED_EPOCH="$SERVICE_STARTED_EPOCH" \
  GH2_INTERNAL_DEADLINE_SECONDS="$INTERNAL_DEADLINE_SECONDS" \
  GH2_WRITER_BUDGET_SECONDS="$WRITER_BUDGET_SECONDS" \
  GH2_DIAGNOSTICS_BUDGET_SECONDS="$DIAGNOSTICS_BUDGET_SECONDS" \
    .venv/bin/python - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

path = Path(os.environ["GH2_STATUS_PATH"])
stage_path = Path(os.environ["GH2_STAGE_PATH"])


def read_json(candidate: Path) -> dict:
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


previous = read_json(path)
stage = read_json(stage_path)
now = datetime.now(UTC)
status = os.environ["GH2_STATUS"]
stage_started_at = parse_time(stage.get("stage_started_at") or stage.get("generated_at"))
last_success = previous.get("last_successful_completion")
if status == "COMPLETE" and previous.get("status") != "COMPLETE":
    last_success = now.isoformat()
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
    "last_successful_completion": last_success,
    "current_stage": stage.get("stage"),
    "current_stage_generated_at": stage.get("generated_at"),
    "current_stage_elapsed_seconds": (
        round(max(0.0, (now - stage_started_at).total_seconds()), 3)
        if stage_started_at is not None
        else None
    ),
    "stage_timings": list(stage.get("stage_timings") or []),
    "diagnostics_status": os.environ.get("GH2_DIAGNOSTICS_STATUS") or "NOT_STARTED",
    "diagnostics_runtime_seconds": float(
        os.environ.get("GH2_DIAGNOSTICS_RUNTIME_SECONDS") or 0
    ),
    "budgets": {
        "internal_deadline_seconds": int(os.environ["GH2_INTERNAL_DEADLINE_SECONDS"]),
        "writer_seconds": int(os.environ["GH2_WRITER_BUDGET_SECONDS"]),
        "diagnostics_seconds": int(os.environ["GH2_DIAGNOSTICS_BUDGET_SECONDS"]),
    },
    "paper_order_creation_enabled": False,
    "live_execution_enabled": False,
    "autopilot_enabled": False,
}
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
temporary.replace(path)
PY
  case "$status" in
    COMPLETE|FAILED|DEFERRED)
      FINAL_STATUS_WRITTEN=1
      ;;
  esac
}

finalize_writer_runtime() {
  if (( WRITER_STARTED_EPOCH > 0 )); then
    WRITER_RUNTIME_SECONDS=$(( $(date +%s) - WRITER_STARTED_EPOCH ))
  fi
}

handle_termination() {
  trap - TERM INT
  finalize_writer_runtime
  if (( CORE_COMPLETE == 1 )); then
    DIAGNOSTICS_STATUS=TIMED_OUT
    write_scheduler_status "COMPLETE"
  else
    write_scheduler_status "FAILED" "SERVICE_TIMEOUT_OR_TERMINATION"
  fi
  exit 143
}

handle_exit() {
  local command_status=$?
  trap - EXIT TERM INT
  if (( command_status != 0 && FINAL_STATUS_WRITTEN == 0 )); then
    finalize_writer_runtime
    write_scheduler_status "FAILED" "UNEXPECTED_SERVICE_EXIT_${command_status}"
  fi
  exit "$command_status"
}

trap handle_termination TERM INT
trap handle_exit EXIT

write_scheduler_status "STAGING"

# Network fetches stage to files before the shared SQLite writer lock is acquired.
if ! timeout --signal=TERM --kill-after=5s 30s \
  .venv/bin/kalshi-bot gh2-stage-crypto-quotes \
    --staging-dir "$GH2_ROOT/crypto-staging" \
    --symbols BTC,ETH,SOL,XRP,DOGE \
    --sources coinbase \
    --max-workers 4; then
  write_scheduler_status "FAILED" "QUOTE_STAGE_FAILED_OR_TIMED_OUT"
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

remaining=$(remaining_service_seconds)
writer_timeout=$WRITER_BUDGET_SECONDS
if (( remaining <= 5 )); then
  write_scheduler_status "FAILED" "GH2_INTERNAL_DEADLINE_EXCEEDED"
  exit 1
fi
if (( writer_timeout > remaining - 5 )); then
  writer_timeout=$(( remaining - 5 ))
fi

write_scheduler_status "RUNNING"
WRITER_STARTED_EPOCH=$(date +%s)
set +e
timeout --signal=TERM --kill-after=5s "${writer_timeout}s" \
  .venv/bin/kalshi-bot gh2-single-writer-decision-refresh \
    --apply \
    --output-dir "$GH2_ROOT/reports" \
    --reports-dir "$APP_PATH/reports" \
    --crypto-staging-dir "$GH2_ROOT/crypto-staging" \
    --gh1-staging-dir "$GH1_ROOT/staging" \
    --candidate-manifest-path "$GH1_ROOT/watch/actionable_tickers.json" \
    --candidate-limit 40 \
    --active-link-limit 24 \
    --forecast-limit 24 \
    --opportunity-limit 20 \
    --freshness-minutes 15 \
    --soak-cycles-required 24 \
    --guard-active-writer
command_status=$?
set -e
finalize_writer_runtime
if (( command_status != 0 )); then
  case "$command_status" in
    124|137|143)
      write_scheduler_status "FAILED" "GH2_INTERNAL_DEADLINE_EXCEEDED"
      ;;
    *)
      write_scheduler_status "FAILED" "GH2_DECISION_REFRESH_FAILED"
      ;;
  esac
  exit "$command_status"
fi

CORE_COMPLETE=1
DIAGNOSTICS_STATUS=PENDING
write_scheduler_status "COMPLETE"

# Runtime diagnostics are read-only and deliberately run after releasing the writer lock.
flock -u 9
exec 9>&-
remaining=$(remaining_service_seconds)
diagnostics_timeout=$DIAGNOSTICS_BUDGET_SECONDS
if (( diagnostics_timeout > remaining - 5 )); then
  diagnostics_timeout=$(( remaining - 5 ))
fi
if (( diagnostics_timeout <= 0 )); then
  DIAGNOSTICS_STATUS=SKIPPED_DEADLINE
  write_scheduler_status "COMPLETE"
  exit 0
fi

DIAGNOSTICS_STATUS=RUNNING
write_scheduler_status "COMPLETE"
diagnostics_started=$(date +%s)
set +e
timeout --signal=TERM --kill-after=5s "${diagnostics_timeout}s" \
  .venv/bin/kalshi-bot roadmap-runtime-reports \
    --reports-root "$APP_PATH/reports" \
    --candidate-manifest-path "$GH1_ROOT/watch/actionable_tickers.json" \
    --freshness-minutes 15 \
    --market-limit 40 \
    --paper-order-limit 1000 \
    --scope-limit 40
diagnostics_command_status=$?
set -e
DIAGNOSTICS_RUNTIME_SECONDS=$(( $(date +%s) - diagnostics_started ))
case "$diagnostics_command_status" in
  0)
    DIAGNOSTICS_STATUS=COMPLETE
    ;;
  124|137|143)
    DIAGNOSTICS_STATUS=TIMED_OUT
    ;;
  *)
    DIAGNOSTICS_STATUS=FAILED
    ;;
esac
write_scheduler_status "COMPLETE"
