#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
DB=/var/lib/kalshi-bot/kalshi_phase1.db

marker=$(sqlite3 "$DB" 'SELECT version_num FROM alembic_version;')
[[ "$marker" == '20260624_0011' ]]
table=$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='market_legs';")
[[ "$table" == 'market_legs' ]]
columns=$(sqlite3 "$DB" 'SELECT count(*) FROM pragma_table_info("market_legs");')
[[ "$columns" -gt 0 ]]

systemctl start kalshi-ui.service
systemctl start kalshi-multicategory-refresh-scheduler.timer

printf 'marker=%s\nmarket_legs_present=true\nmarket_legs_columns=%s\nui=%s\nscheduler=%s\n' \
  "$marker" "$columns" \
  "$(systemctl is-active kalshi-ui.service)" \
  "$(systemctl is-active kalshi-multicategory-refresh-scheduler.timer)"

.venv/bin/alembic current
.venv/bin/kalshi-bot db-writer-monitor --json
.venv/bin/kalshi-bot db-locks
