#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/kalshi-bot/kalshi-bot.env
set +a
cd /opt/kalshi-predictive-bot
echo UTC=$(date -u +%FT%TZ)
systemctl is-active kalshi-ui.service kalshi-r5-watcher.service \
  kalshi-multicategory-refresh-scheduler.timer
./.venv/bin/kalshi-bot db-writer-monitor --json-output /tmp/prov12_writer.json || true
./.venv/bin/kalshi-bot db-locks --json-output /tmp/prov12_locks.json || true
journalctl -k --since '2026-07-17 05:22:22 UTC' --no-pager | \
  grep -Ei 'oom|out of memory|killed process' || true
grep -E '^(PROV12_DECISION_TRACE_PREVIEW_ENABLED|EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' \
  /etc/kalshi-bot/kalshi-bot.env || true
./.venv/bin/python - <<'PY'
import json
alerts=json.load(open('reports/phase_prov12/prov12_drift_alerts.json'))
trace=json.load(open('reports/phase_prov12/prov12_sample_trace.json'))
print(json.dumps({'alert_summary': alerts['summary'], 'sample_ticker': trace['ticker'],
                  'sample_status': trace['status'], 'sample_alerts': trace['alerts'],
                  'stages': len(trace['stages']), 'read_only': trace['read_only'],
                  'database_writes': trace['database_writes']}, sort_keys=True))
PY
sha256sum reports/phase_prov12/*.json
