#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/kalshi-bot/kalshi-bot.env
set +a
echo UTC=$(date -u +%FT%TZ)
grep -E '^(EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' \
  /etc/kalshi-bot/kalshi-bot.env || true
systemctl show kalshi-multicategory-refresh-scheduler.service kalshi-r5-watcher.service \
  -p Id -p ActiveState -p SubState -p Result -p MainPID -p ExecMainStartTimestamp \
  -p ExecMainExitTimestamp -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax \
  -p TimeoutStartUSec -p RuntimeMaxUSec
systemctl list-timers kalshi-multicategory-refresh-scheduler.timer --all --no-pager
./.venv/bin/kalshi-bot db-writer-monitor --json-output /tmp/prov10_writer.json || true
./.venv/bin/kalshi-bot db-locks --json-output /tmp/prov10_locks.json || true
/opt/kalshi-predictive-bot/.venv/bin/python /tmp/prov9_chain_verify.py
journalctl -u kalshi-multicategory-refresh-scheduler.service --since=-30min --no-pager -n 50
journalctl -k --since=-2h --no-pager | grep -Ei 'oom|out of memory|killed process' || true
