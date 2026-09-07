#!/usr/bin/env bash
set -euo pipefail

cd /opt/kalshi-predictive-bot
LOCK_FILE=/tmp/kalshi-multicategory-refresh-scheduler.lock
KALSHI_BOT=${KALSHI_BOT:-.venv/bin/kalshi-bot}
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo '[phase3bb-r35] scheduler already running; exiting cleanly'
  exit 0
fi

writer_clear() {
  local monitor_output
  if ! monitor_output=$("${KALSHI_BOT}" db-writer-monitor --json 2>/dev/null); then
    echo '[phase3bb-r35] db-writer-monitor failed; skip writer-gated job' >&2
    return 1
  fi
  MONITOR_OUTPUT="${monitor_output}" python3 -c '
import json, os, re, sys
text = os.environ.get("MONITOR_OUTPUT", "")
text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
decoder = json.JSONDecoder()
for idx, char in enumerate(text):
    if char != "{":
        continue
    try:
        data, _end = decoder.raw_decode(text[idx:])
    except json.JSONDecodeError:
        continue
    raise SystemExit(0 if data.get("safe_to_start_write") else 1)
print("[phase3bb-r35] db-writer-monitor JSON parse failed; skip writer-gated job", file=sys.stderr)
raise SystemExit(1)'
}

run_job() {
  local job_id="$1"
  local writer_capable="$2"
  shift 2
  if [[ "${writer_capable}" == "true" ]] && ! writer_clear; then
    echo "[phase3bb-r35] Writer active; skip writer-gated job ${job_id}"
    return 0
  fi
  echo "[phase3bb-r35] running ${job_id}"
  local output status
  set +e
  output=$("$@" 2>&1)
  status=$?
  set -e
  if [[ -n "${output}" ]]; then
    printf '%s
' "${output}"
  fi
  if [[ "${status}" -ne 0 ]]; then
    if [[ "${writer_capable}" == "true" ]] && printf '%s
' "${output}" | grep -Eq 'Status: BUSY_WRITER|Database is busy|safe_to_start_write[^A-Za-z0-9_:-]*false'; then
      echo "[phase3bb-r35] Writer became active during ${job_id}; clean skip for retry"
      return 0
    fi
    return "${status}"
  fi
}

# cadence_minutes=15 category=system
run_job operations_readiness_monitor false .venv/bin/kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness --output-dir reports/phase3bb_r33 --reports-dir reports

# cadence_minutes=15 category=all
run_job unified_paper_gate false .venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports --limit-per-category 50

# cadence_minutes=30 category=weather-catalog
run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; .venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH; .venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; .venv/bin/kalshi-bot ingest-weather --location-key new_york; .venv/bin/kalshi-bot build-weather-features --location-key new_york; .venv/bin/kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3'

# cadence_minutes=30 category=weather
run_job weather_fast_lane true .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports

# cadence_minutes=360 category=all
run_job free_source_inventory false .venv/bin/kalshi-bot phase3bb-r3-free-source-inventory --output-dir reports/phase3bb_r3 --reports-dir reports

# cadence_minutes=720 category=economic
run_job economic_parser_backfill_review false .venv/bin/kalshi-bot phase3bb-r4-economic-parser-backfill --output-dir reports/phase3bb_r4 --reports-dir reports

# cadence_minutes=720 category=news
run_job news_event_discovery_review false .venv/bin/kalshi-bot phase3bb-r7-news-event-discovery --output-dir reports/phase3bb_r7 --reports-dir reports

# cadence_minutes=720 category=sports
run_job sports_provenance_review false .venv/bin/kalshi-bot phase3bb-r6-sports-provenance-repair --output-dir reports/phase3bb_r6 --reports-dir reports

# cadence_minutes=360 category=coverage
run_job coverage_doctor_review false .venv/bin/kalshi-bot market-coverage-doctor --output-dir reports/market_coverage

# cadence_minutes=15 category=crypto
run_job crypto_background_status false .venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5
