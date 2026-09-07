#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a

[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
systemctl stop kalshi-ui.service

for _ in $(seq 1 20); do
  holders=$(.venv/bin/kalshi-bot db-writer-monitor --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["holder_count"])')
  [[ "$holders" -eq 0 ]] && break
  sleep 1
done
[[ "$holders" -eq 0 ]]
.venv/bin/kalshi-bot db-locks

DB=/var/lib/kalshi-bot/kalshi_phase1.db
ticker=$(sqlite3 "$DB" 'SELECT ticker FROM market_snapshots ORDER BY captured_at DESC, id DESC LIMIT 1;')
[[ -n "$ticker" ]]
before_forecasts=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM forecasts;')
before_events=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM runtime_provenance_events;')

flock -n /var/lib/kalshi-bot/prov7-certify.lock \
  .venv/bin/kalshi-bot forecast --model market_implied_v1 --limit 1 --ticker "$ticker"

after_forecasts=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM forecasts;')
after_events=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM runtime_provenance_events;')
[[ "$after_forecasts" -gt "$before_forecasts" ]]
[[ "$after_events" -gt "$before_events" ]]

sqlite3 -header -column "$DB" <<SQL
SELECT id,ticker,model_name,forecasted_at FROM forecasts ORDER BY id DESC LIMIT 1;
SELECT id,event_key,stage,forecast_id,ticker,model_name,model_version,
       previous_digest,provenance_digest
FROM runtime_provenance_events ORDER BY id DESC LIMIT 1;
SQL
printf 'ticker=%s\nforecast_rows_before=%s\nforecast_rows_after=%s\nevents_before=%s\nevents_after=%s\n' \
  "$ticker" "$before_forecasts" "$after_forecasts" "$before_events" "$after_events"

systemctl start kalshi-ui.service
systemctl start kalshi-multicategory-refresh-scheduler.timer
