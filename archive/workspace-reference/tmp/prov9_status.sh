#!/usr/bin/env bash
set -euo pipefail
echo UTC=$(date -u +%FT%TZ)
systemctl show kalshi-r5-watcher.service kalshi-multicategory-refresh-scheduler.service \
  -p Id -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
  -p Result -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax
free -h
journalctl -u kalshi-multicategory-refresh-scheduler.service --since '20 minutes ago' \
  --no-pager -n 40
journalctl -k --since '30 minutes ago' --no-pager | grep -Ei 'oom|out of memory|killed process' || true
grep -E '^(EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' /etc/kalshi-bot/kalshi-bot.env || true
