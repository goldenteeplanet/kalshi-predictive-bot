#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a

systemctl stop kalshi-multicategory-refresh-scheduler.timer
systemctl stop kalshi-multicategory-refresh-scheduler.service || true
systemctl stop kalshi-ui.service

for _ in $(seq 1 30); do
  holders=$(.venv/bin/kalshi-bot db-writer-monitor --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["holder_count"])')
  [[ "$holders" -eq 0 ]] && break
  sleep 1
done

.venv/bin/kalshi-bot db-writer-monitor --json
.venv/bin/kalshi-bot db-locks
