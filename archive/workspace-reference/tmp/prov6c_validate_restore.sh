#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a

DB=/var/lib/kalshi-bot/kalshi_phase1.db
echo SCHEMA
sqlite3 "$DB" <<'SQL'
SELECT version_num FROM alembic_version;
SELECT name, type FROM sqlite_master WHERE name='market_legs';
PRAGMA table_info(market_legs);
SQL

echo QUICK_CHECK
sqlite3 "$DB" 'PRAGMA quick_check(1);'

systemctl start kalshi-ui.service
systemctl start kalshi-multicategory-refresh-scheduler.timer
echo SERVICES
systemctl is-active kalshi-ui.service
systemctl is-active kalshi-multicategory-refresh-scheduler.timer

echo FINAL_WRITER
.venv/bin/kalshi-bot db-writer-monitor --json
echo FINAL_LOCKS
.venv/bin/kalshi-bot db-locks
