#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
echo PROV9_PREFLIGHT_UTC=$(date -u +%FT%TZ)
grep -E '^(EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' /etc/kalshi-bot/kalshi-bot.env || true
systemctl show kalshi-r5-watcher.service kalshi-multicategory-refresh-scheduler.service \
  -p Id -p ActiveState -p SubState -p MainPID -p MemoryCurrent -p MemoryPeak \
  -p MemoryHigh -p MemoryMax -p TimeoutStartUSec -p RuntimeMaxUSec
free -h
./.venv/bin/kalshi-bot db-writer-monitor --json-output /tmp/prov9_writer_monitor.json || true
./.venv/bin/kalshi-bot db-locks --json-output /tmp/prov9_db_locks.json || true
cat /tmp/prov9_writer_monitor.json /tmp/prov9_db_locks.json 2>/dev/null || true
ps -o pid,ppid,rss,vsz,etime,cmd -p "$(systemctl show -p MainPID --value kalshi-r5-watcher.service)" -p "$(systemctl show -p MainPID --value kalshi-multicategory-refresh-scheduler.service)"
systemctl cat kalshi-r5-watcher.service kalshi-multicategory-refresh-scheduler.service
