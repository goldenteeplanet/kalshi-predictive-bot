#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/kalshi-bot/kalshi-bot.env
set +a
cd /opt/kalshi-predictive-bot
echo UTC=$(date -u +%FT%TZ)
systemctl is-active kalshi-ui.service kalshi-r5-watcher.service \
  kalshi-multicategory-refresh-scheduler.timer
./.venv/bin/kalshi-bot db-writer-monitor --json-output /tmp/prov11_writer.json || true
./.venv/bin/kalshi-bot db-locks --json-output /tmp/prov11_locks.json || true
journalctl -k --since '2026-07-17 05:22:22 UTC' --no-pager | \
  grep -Ei 'oom|out of memory|killed process' || true
grep -E '^(PROV11_DASHBOARD_PREVIEW_ENABLED|EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' \
  /etc/kalshi-bot/kalshi-bot.env || true
sha256sum reports/phase_prov11/prov11_provenance_diagnostics.json
