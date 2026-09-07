#!/usr/bin/env bash
set -euo pipefail
date -u
grep -E '^RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=|^EXECUTION_ENABLED=' /etc/kalshi-bot/kalshi-bot.env
systemctl show kalshi-multicategory-refresh-scheduler.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus -p MainPID
systemctl is-active kalshi-ui.service || true
systemctl is-active kalshi-multicategory-refresh-scheduler.timer || true
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
.venv/bin/kalshi-bot db-writer-monitor --json
sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db \
  'SELECT COUNT(*), MIN(stage), MAX(stage) FROM runtime_provenance_events;'
