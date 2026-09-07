#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/kalshi-bot/kalshi-bot.env
set +a
echo UTC=$(date -u +%FT%TZ)
systemctl show kalshi-multicategory-refresh-scheduler.service kalshi-r5-watcher.service \
  -p Id -p ActiveState -p SubState -p Result -p MainPID -p ExecMainStartTimestamp \
  -p ExecMainExitTimestamp -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax
systemctl list-timers kalshi-multicategory-refresh-scheduler.timer --all --no-pager
echo SCHEDULER_TRANSITIONS
journalctl -u kalshi-multicategory-refresh-scheduler.service \
  --since '2026-07-17 05:22:22 UTC' --no-pager | \
  grep -E 'Starting |Started |Deactivated successfully|Failed with result|start operation timed out|Consumed ' || true
echo RECENT_STAGES
journalctl -u kalshi-multicategory-refresh-scheduler.service --since=-25min --no-pager -n 25
echo NEW_OOM
journalctl -k --since '2026-07-17 05:22:22 UTC' --no-pager | \
  grep -Ei 'oom|out of memory|killed process' || true
/opt/kalshi-predictive-bot/.venv/bin/python /tmp/prov9_chain_verify.py
./.venv/bin/kalshi-bot db-writer-monitor --json-output /tmp/prov10_progress_writer.json || true
grep -E '^(EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' \
  /etc/kalshi-bot/kalshi-bot.env || true
