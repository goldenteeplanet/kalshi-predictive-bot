#!/usr/bin/env bash
set -euo pipefail
date -u
echo KERNEL
journalctl -k --since '2026-07-17 03:55:00' --no-pager | grep -Ei 'oom|out of memory|killed process' || true
echo SERVICES
systemctl list-units --type=service --all --no-pager | grep -E 'kalshi.*(r5|crypto|multicategory)' || true
echo EVENTS
sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db \
  'SELECT stage,model_name,COUNT(*) FROM runtime_provenance_events GROUP BY stage,model_name ORDER BY stage,model_name;'
echo CHECK
sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db 'PRAGMA quick_check(1);'
echo LOCKS
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
.venv/bin/kalshi-bot db-writer-monitor --json
.venv/bin/kalshi-bot db-locks
