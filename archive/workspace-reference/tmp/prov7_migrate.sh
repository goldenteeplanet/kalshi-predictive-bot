#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a

[[ "${RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED:-false}" == false ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
holders=$(.venv/bin/kalshi-bot db-writer-monitor --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["holder_count"])')
[[ "$holders" -eq 0 ]]
.venv/bin/kalshi-bot db-locks

flock -n /var/lib/kalshi-bot/prov7-alembic.lock \
  env RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=false \
  .venv/bin/alembic upgrade 20260716_0012

current=$(.venv/bin/alembic current 2>/dev/null | tail -1)
[[ "$current" == '20260716_0012 (head)' ]]
table=$(sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db \
  "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_provenance_events';")
[[ "$table" == runtime_provenance_events ]]
count=$(sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db 'SELECT COUNT(*) FROM runtime_provenance_events;')
[[ "$count" -eq 0 ]]
printf 'revision=%s\ntable=%s\ninitial_rows=%s\n' "$current" "$table" "$count"
