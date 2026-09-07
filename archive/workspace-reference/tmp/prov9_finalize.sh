#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/kalshi-bot/kalshi-bot.env
set +a
journalctl -u prov9-r8-final2.service --no-pager -n 60
/opt/kalshi-predictive-bot/.venv/bin/python /tmp/prov9_chain_verify.py
systemctl start kalshi-multicategory-refresh-scheduler.timer
systemctl show kalshi-multicategory-refresh-scheduler.timer \
  -p ActiveState -p SubState -p NextElapseUSecRealtime
journalctl -k --since=-45min --no-pager | grep -Ei 'oom|out of memory|killed process' || true
/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot db-writer-monitor \
  --json-output /tmp/prov9_final_writer.json || true
/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot db-locks \
  --json-output /tmp/prov9_final_locks.json || true
grep -E '^(EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' \
  /etc/kalshi-bot/kalshi-bot.env || true
